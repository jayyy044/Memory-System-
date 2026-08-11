import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

from membench.corpus.extract import BenchTask
from membench.driver import _CAP_ARGS, _CONTAINER_WORKDIR, _docker_run, _ensure_image
from membench.workspace import _submodule_paths

# S3/D54: docker/collect_deps.py, reused (not reimplemented) - same
# validated-PEP-508 parser, run on the HOST against a frozen base_sha
# snapshot instead of inside the container against /workspace.
_COLLECT_DEPS_SCRIPT = Path(__file__).resolve().parent.parent / "docker" / "collect_deps.py"
_CONTAINER_FROZEN_DEPS = "/run/membench/deps/frozen-deps.txt"

# R2/D50: a trusted config file baked into the image (never the bind-mounted
# /workspace) so a workspace pyproject.toml's [tool.pytest.ini_options] /
# addopts is never consulted for scoring - `-c FILE` replaces pytest's
# config-file auto-discovery outright, it doesn't merge with it. Verified
# live: addopts = "--collect-only" in a workspace pyproject.toml silently
# turned every scored run into a no-op without `-c`; tests actually execute
# with it present.
#
# JUDGEMENT CALL (fix round 3, reviewer-requested): this hard substitution
# also discards LEGITIMATE gold ini settings a real repo might declare -
# asyncio_mode, filterwarnings, markers, testpaths, xfail_strict. liquid
# needs none of these, so this is genuinely untested against a repo that
# does. Decision: keep the hard substitution rather than merge gold's ini
# keys in. Reasons: (1) a safe merge means parsing THREE dialects
# (pyproject.toml's [tool.pytest.ini_options] is TOML; pytest.ini/tox.ini/
# setup.cfg are INI) while explicitly excluding `addopts` specifically -
# that's new parsing surface introduced under an already-dense review round,
# and getting the exclusion subtly wrong reopens exactly what `-c` exists to
# close; (2) the failure mode of NOT merging is the fail-closed, visible
# kind (a repo needing asyncio_mode collects/runs its async tests oddly,
# operator sees implausible results and investigates) rather than a silent
# miscategorization - the same "recoverable over silent" tradeoff R1 already
# made. Tracked as a named limitation: a corpus repo whose test suite
# depends on pytest.ini/pyproject.toml settings beyond defaults needs this
# revisited before onboarding, not discovered by a wrong score.
_TRUSTED_PYTEST_INI = "/usr/local/etc/membench-pytest.ini"

# D30: pytest runs INSIDE the sealed container, not on the host. Two reasons:
# (a) liquid's dependencies are installed by seal-egress.sh (collect_deps.py
# + pip, D28/D32/D36) while the network is still open - a host-side `pytest`
# never sees them and reproduces the 113 ModuleNotFoundError Task 13 fixed;
# (b) VALIDITY - scoring must run in the same environment the agent ran in,
# or a pass/fail verdict doesn't correspond to what the agent actually
# experienced. No network is needed for scoring, so this reuses run_agent's
# exact image/caps/entrypoint and lets the seal close all the way (no
# ANTHROPIC_HOST to allowlist here).
#
# --json-report (pytest-json-report) is added to the image alongside pytest
# (docker/Dockerfile) rather than reconstructed from junit-xml classnames or
# scraped from `-rA` terminal text: it's the one option that returns exact
# pytest nodeids 1:1, matching fail_to_pass/pass_to_pass verbatim, with no
# reconstruction logic to get subtly wrong on parametrized/nested-class ids.


class RunTestsError(RuntimeError):
    """Raised instead of returning a misleading result:
    - pytest never wrote a report at all (crashed before --json-report-file,
      e.g. a bad container/image) - the brief's `return {}` on this path is
      indistinguishable from "every test failed"; that ambiguity is exactly
      what breaks scoring, so it's an exception instead.
    - D31: a specific node id was requested but never appears in the report
      (uncollectible - e.g. a module that fails to import). A
      fail_to_pass/pass_to_pass id landing there must not silently score as
      "failed" for every arm; it must be surfaced as unusable.
    """


# R1/D49: a DENYLIST here is the catastrophic direction, reverted from the
# previous round. pytest-json-report takes `outcome` VERBATIM from whatever
# `pytest_report_teststatus` hook returns (plugin.py:197-200) - an OPEN set,
# not an enumeration; any plugin, or a planted conftest.py, can return any
# string. Under a denylist, an unrecognized string reads as True (passed) -
# a SILENT FALSE-SOLVED, which nothing downstream catches. Under an
# allowlist, an unrecognized string reads as False (not-passed) - a visible
# false-UNSOLVED, which is recoverable (a human/gate check sees an
# implausible 0% and investigates). Fail closed.
#
# "xfailed" (expected failure occurred exactly as declared) is included -
# that's the test behaving correctly, the same non-anomalous case as
# "skipped"/"subtests passed". "xpassed" (expected failure did NOT occur) is
# deliberately EXCLUDED: it's the anomalous case - the codebase's own
# declaration of expected behavior was contradicted - and its meaning for
# scoring is genuinely ambiguous (could be the fix's doing, could be
# flakiness). Fail-closed applies here too: uncertain -> not-passed.
_PASSING_OUTCOMES = {"passed", "skipped", "subtests passed", "xfailed"}

# S4: pytest-json-report seeds a test item's top-level `outcome` optimistically
# as "passed" (serialize.make_testitem) and only overwrites it when
# `pytest_report_teststatus` - a firstresult hook, so any ONE plugin can
# supply the answer for everyone - returns something other than "passed"/""
# (plugin.py's pytest_runtest_logreport: `if outcome not in ['passed', ''])`.
# A plugin (registered from a dependency, not just a conftest.py file - S3
# closes the delivery vector for OUR corpus but this is a mapping-level gap
# independent of how the hook got there) returning "" for a genuinely
# failing test leaves the top-level field reading "passed" - a string
# IDENTICAL to a real pass; no allowlist tuning can tell them apart from
# that field alone. `report.outcome`, embedded per-phase (setup/call/
# teardown) via `make_teststage`, is different: it's pytest CORE's own
# TestReport.outcome, set in `pytest_runtest_makereport` BEFORE
# pytest_report_teststatus ever runs, and is not itself passed through that
# hook. Cross-checking against it defeats a hook that only manipulates the
# top-level categorization.
def _test_passed(test: dict) -> bool:
    if test.get("outcome") not in _PASSING_OUTCOMES:
        return False
    return not any(test.get(stage, {}).get("outcome") == "failed" for stage in ("setup", "call", "teardown"))

# S2/D53: standalone config-shaped filenames, matched anywhere in the tree
# regardless of directory - conftest.py at any level (pytest always imports
# these regardless of ini source); sitecustomize.py (the interpreter
# auto-imports this from any dir on sys.path, and /workspace is on sys.path
# by construction); pytest.ini/tox.ini/setup.cfg (belt-and-suspenders on top
# of `-c`, in case anything ever invokes pytest without it).
_DISCARD_NAMES = {"conftest.py", "sitecustomize.py", "pytest.ini", "tox.ini", "setup.cfg"}


def _is_test_named(path: str) -> bool:
    name = Path(path).name
    return name.startswith("test_") or name.endswith("_test.py")


def _looks_like_config_name(path: str) -> bool:
    return Path(path).name in _DISCARD_NAMES


def _cat_file(repo_dir: Path, ref: str, path: str) -> bytes | None:
    """A pure object-database read - no hooks, no smudge/clean filters (those
    only apply during working-tree materialization, i.e. checkout/merge/add,
    never a blob read). Returns None if `path` doesn't exist at `ref`
    (mirrors collect_deps.py's own `if path.exists()` pattern).

    S1/D52/D57: `repo_dir` must NEVER be the agent-writable workspace -
    see `_reset_test_surface`'s docstring for why."""
    r = subprocess.run(["git", "cat-file", "-p", f"{ref}:{path}"], cwd=repo_dir, capture_output=True)
    return r.stdout if r.returncode == 0 else None


def _require_ref(repo_dir: Path, sha: str) -> None:
    r = subprocess.run(["git", "cat-file", "-e", f"{sha}^{{commit}}"], cwd=repo_dir, capture_output=True)
    if r.returncode != 0:
        raise RunTestsError(
            f"reference repo {repo_dir} does not contain commit {sha!r} - refusing to fall back to "
            f"any operation against the workspace's own (agent-writable, untrusted) git state"
        )


def _frozen_deps_file(task: BenchTask, reference_repo: Path, scratch: Path) -> Path:
    """S3/D54: dependency install at SCORING time must not be re-derived from
    the agent's (possibly tampered) live workspace - demonstrated: a
    workspace requirements.txt of `pytest-timeout==2.3.1` registered a
    plugin inside the scoring run, and `pytest==7.4.4` DOWNGRADED the
    scorer's own pinned pytest. Freezes the dependency set from task.base_sha
    instead - what was actually on disk when the agent's episode started -
    read from `reference_repo` (D57: a trusted, agent-UNwritable clone, e.g.
    the full-history fixture; never the workspace itself, see
    `_reset_test_surface`) and runs collect_deps.py's SAME validated-PEP-508
    parser against that frozen snapshot. Safe to run on the host (not root,
    no pip, tomllib+regex only, same as always)."""
    _require_ref(reference_repo, task.base_sha)
    for name in ("pyproject.toml", "requirements.txt"):
        blob = _cat_file(reference_repo, task.base_sha, name)
        if blob is not None:
            (scratch / name).write_bytes(blob)
    proc = subprocess.run(
        [sys.executable, str(_COLLECT_DEPS_SCRIPT), str(scratch)],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise RunTestsError(f"failed to freeze scoring dependency set from base_sha: {proc.stderr}")
    deps_file = scratch / "frozen-deps.txt"
    deps_file.write_text(proc.stdout)
    return deps_file


def _reset_test_surface(workdir: Path, task: BenchTask, reference_repo: Path) -> None:
    """D45(a) + R2/S2: an agent's edits to SOURCE carry into scoring; its
    edits to anything pytest itself loads (tests, conftest.py, pytest config
    files, sitecustomize.py) do not.

    S2/D53: matches by PATH, not filename. `_is_test_named` only bootstraps
    which top-level directories ARE "test roots" (any dir gold puts a
    test_*.py/*_test.py file under, e.g. "tests") - every file under a test
    root is then in scope regardless of its own name. Filename-only matching
    missed `tests/__init__.py` (present in gold, so pass 1 never touched it,
    pass 2 never deleted it) even though pytest imports it for every
    `tests/*` module - demonstrated live: monkeypatching
    `_pytest.python.Function.runtest` from inside it scored solved=True with
    the file still present. Any module a gold test imports is test surface,
    named like a test or not.

    S1/D52 + D57: CRITICAL, twice. Round 3 replaced `git checkout` (fires
    `post-checkout`) with `git cat-file` - that closed the checkout-specific
    hook sub-vector but ran `git remote add`/`fetch` against the workspace's
    OWN `.git/config`, which is agent-writable and never reset. A
    `protocol.ext.allow=always` + `url.<ext-command>.insteadOf=<clone url>`
    pair in that config makes ANY git command that touches a remote matching
    that URL execute an arbitrary command as this host process (uid 501),
    outside the container, outside the seal, with full network - reproduced
    independently: the `git fetch` attempt still fails ("Could not read from
    remote repository") but the planted command runs regardless, before git
    ever gets to fail. `-c core.hooksPath=/dev/null` does nothing against
    this - it's not a hooks mechanism, it's git's own remote-helper dispatch.

    The categorical fix: this function invokes NO git command with `cwd`
    anywhere under `workdir`, for any purpose, ever. Gold content comes
    exclusively from `reference_repo` - a trusted, agent-UNwritable clone
    with `task.fix_sha` already in its local history (the full-history
    fixture in tests; whatever equivalent reference clone corpus
    construction maintains in production). No fetch, no remote, nothing that
    reads workspace config. `_require_ref` (called by the caller, `run_tests`,
    before this runs) fails loudly if `reference_repo` doesn't actually have
    `fix_sha` rather than falling back to touching the workspace.

    `workdir` here is used ONLY as a destination for `Path.write_bytes`/
    `os.walk`/`Path.unlink` - plain filesystem operations, never git.
    """
    _require_ref(reference_repo, task.fix_sha)
    gold_paths_all = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", task.fix_sha],
        cwd=reference_repo, capture_output=True, text=True, check=True,
    ).stdout.splitlines()
    test_roots = {p.split("/", 1)[0] for p in gold_paths_all if _is_test_named(p)}
    gold_surface = {
        p for p in gold_paths_all
        if p.split("/", 1)[0] in test_roots or _looks_like_config_name(p)
    }
    for rel in sorted(gold_surface):
        blob = _cat_file(reference_repo, task.fix_sha, rel)
        if blob is None:
            continue  # shouldn't happen - rel came from this same tree - but never crash restoration over one path
        dest = workdir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(blob)

    # `gold_surface` (superproject `git ls-tree -r`) never descends into a
    # submodule's own working tree - git treats a submodule as a single
    # "commit" entry, not expanded into its files. os.walk knows nothing
    # about that boundary, so without this exclusion the delete pass could
    # wipe legitimate content a submodule happens to share a test-root name
    # with - same submodule-boundary precedent as workspace.py's own
    # instruction-file walk.
    sub_dirs = _submodule_paths(workdir)
    for root, dirnames, filenames in os.walk(workdir):
        root_path = Path(root)
        if any(root_path.resolve() == sd or sd in root_path.resolve().parents for sd in sub_dirs):
            dirnames[:] = []
            continue
        if ".git" in dirnames:
            dirnames.remove(".git")
        for name in filenames:
            full = root_path / name
            rel = full.relative_to(workdir).as_posix()
            in_surface = rel.split("/", 1)[0] in test_roots or _looks_like_config_name(rel)
            if in_surface and rel not in gold_surface:
                full.unlink()


def run_tests(
    workdir: Path, task: BenchTask, node_ids: list[str] | None = None, *, reference_repo: Path
) -> dict[str, bool]:
    """Runs pytest for `workdir` inside the sealed container and returns
    {nodeid: passed}. `task` is mandatory, not optional (D45(a)): making
    gold-test restoration something a caller must remember to invoke
    separately is exactly the shape of bug that produced a false `solved`
    in review - restoration happens unconditionally as the first step here,
    so it can't be skipped by a forgetful caller. `node_ids` may be exact
    node ids ("tests/x.py::test_y") or file/dir targets ("tests/x.py");
    only exact node ids (containing "::") are checked for collectibility
    (D31) - a file target legitimately yields zero results if the whole
    file is skipped/filtered, which isn't the failure this guards against.

    `reference_repo` is mandatory too, and deliberately has no default
    (D57): a trusted, agent-UNwritable local clone with full history -
    everything gold content is read from. It replaces the old `url=`
    override, which pointed `git remote add`/`fetch` AT the workspace; that
    entire class of operation is gone now, not just its URL source (S1/D57 -
    see _reset_test_surface's docstring). No implicit fallback to cloning
    from `task.repo` into the workspace exists - a caller with no reference
    clone available must be given one, not left to synthesize one insecurely.
    """
    _reset_test_surface(workdir, task, reference_repo)

    image = _ensure_image()
    report_name = f".membench-report-{uuid.uuid4().hex[:8]}.json"
    report_host = workdir / report_name

    cmd = [
        "python3", "-m", "pytest", "-q",
        "-c", _TRUSTED_PYTEST_INI,  # R2/D50: ignore workspace pytest.ini/tox.ini/setup.cfg/pyproject.toml[tool.pytest.ini_options] entirely - verified live, see module docstring
        # `-c` living outside /workspace shifts pytest's rootdir computation
        # unless pinned explicitly - verified live: nodeids came back as
        # "../usr/local/etc::test_issue_209" instead of "tests/x.py::test_y"
        # without this, silently breaking every nodeid lookup against
        # fail_to_pass/pass_to_pass (and this file's own `missing` check).
        "--rootdir", _CONTAINER_WORKDIR,
        "-p", "no:cacheprovider",  # no .pytest_cache state bleeding across repeated scoring runs on the same bind-mounted workdir
        "-p", "no:randomly",  # brief's original flag, restored: harmless no-op today (plugin not installed), forward-defensive if it ever is - test order must stay deterministic for scoring
        "--hypothesis-seed=0",  # D46: hypothesis (now baked into the image, see Dockerfile) draws fresh examples every run otherwise; a fixed seed makes property-based F2P/P2P results reproducible run to run
        # Verified empirically against liquid (task-4-report.md): WITHOUT
        # this flag, pytest's default behavior is to abort the entire
        # session on ANY collection error and run zero tests - not skip just
        # the broken file(s). A single agent-introduced (or otherwise
        # broken) test file must not zero the whole report.
        "--continue-on-collection-errors",
        "--json-report", f"--json-report-file={_CONTAINER_WORKDIR}/{report_name}",
    ]
    if node_ids:
        cmd.extend(node_ids)

    # S3/D54: mount the frozen (base_sha) dependency spec on its own
    # read-only path outside /workspace, and point seal-egress.sh at it via
    # env var - the entrypoint installs THIS instead of re-deriving from the
    # agent's live (possibly tampered) /workspace when the var is set.
    with tempfile.TemporaryDirectory(prefix="membench-deps-") as deps_scratch:
        deps_file = _frozen_deps_file(task, reference_repo, Path(deps_scratch))
        proc = _docker_run(
            [
                *_CAP_ARGS,
                "-v", f"{workdir}:{_CONTAINER_WORKDIR}",
                "-v", f"{deps_scratch}:/run/membench/deps:ro",
                "-e", f"MEMBENCH_FROZEN_DEPS={_CONTAINER_FROZEN_DEPS}",
                "-e", "TZ=UTC",
                "-e", "PYTHONDONTWRITEBYTECODE=1",
                image,
                *cmd,
            ],
            timeout_s=1800,
        )

    # R4/D51: hypothesis (baked into the image, D46) drops a `.hypothesis/`
    # example database into the bind mount on every run regardless of
    # --hypothesis-seed - agent-writable state that would otherwise persist
    # across repeated scoring runs on the same workdir. Minor (a fixed seed
    # already makes example generation deterministic per run; unproven
    # hazard, 3 matched runs during review), but cheap to close - same
    # cleanup shape as the JSON report file below.
    # ponytail: does not clean up the working tree's git-dirty state left by
    # `_reset_test_surface`'s writes (`M tests/...`) - cosmetic (doesn't
    # execute anything, scoring already re-runs `_reset_test_surface` fresh
    # on every call), upgrade to a plain-filesystem reset here first if a
    # multi-episode re-scoring use case ever depends on a clean tree.
    shutil.rmtree(workdir / ".hypothesis", ignore_errors=True)

    if not report_host.exists():
        raise RunTestsError(
            f"pytest produced no --json-report file (container rc={proc.returncode}); "
            f"stdout tail: {proc.stdout[-2000:]!r} stderr tail: {proc.stderr[-2000:]!r}"
        )
    try:
        data = json.loads(report_host.read_text())
    finally:
        report_host.unlink(missing_ok=True)

    results = {t["nodeid"]: _test_passed(t) for t in data.get("tests", [])}

    requested_exact = [n for n in (node_ids or []) if "::" in n]
    missing = [n for n in requested_exact if n not in results]
    if missing:
        raise RunTestsError(
            f"requested node id(s) not collectible in this environment (not present in "
            f"pytest's own report - see D31): {missing}"
        )
    return results
