import json
import os
import shutil
import subprocess
import uuid
from pathlib import Path

from membench.corpus.extract import BenchTask
from membench.driver import _CAP_ARGS, _CONTAINER_WORKDIR, _docker_run, _ensure_image
from membench.workspace import _submodule_paths

# R2/D50: a trusted config file baked into the image (never the bind-mounted
# /workspace) so a workspace pyproject.toml's [tool.pytest.ini_options] /
# addopts is never consulted for scoring - `-c FILE` replaces pytest's
# config-file auto-discovery outright, it doesn't merge with it. Verified
# live: addopts = "--collect-only" in a workspace pyproject.toml silently
# turned every scored run into a no-op without `-c`; tests actually execute
# with it present.
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

# R2/D50: paths whose mere PRESENCE lets code run or settings apply outside
# pytest's own `-c`-controlled ini (conftest.py at any level - pytest always
# imports these regardless of ini source; sitecustomize.py - the interpreter
# auto-imports this from any dir on sys.path, and /workspace is on sys.path
# by construction; pytest.ini/tox.ini/setup.cfg - belt-and-suspenders on top
# of `-c`, in case anything ever invokes pytest without it) plus the test
# files themselves (D45(a), unchanged from last round).
_DISCARD_NAMES = {"conftest.py", "sitecustomize.py", "pytest.ini", "tox.ini", "setup.cfg"}


def _looks_like_test_surface(path: str) -> bool:
    name = Path(path).name
    return name in _DISCARD_NAMES or name.startswith("test_") or name.endswith("_test.py")


def _reset_test_surface(workdir: Path, task: BenchTask, *, url: str | None = None) -> None:
    """D45(a) + R2/D50: an agent's edits to SOURCE carry into scoring; its
    edits to anything pytest itself loads (tests, conftest.py, pytest
    config files, sitecustomize.py) do not. Two passes:

    1. Checkout every such path PRESENT in the gold tree at task.fix_sha
       (unchanged from last round) - restores tampered gold tests, recreates
       deleted ones.
    2. NEW: delete every such path found ANYWHERE in the current tree that
       is NOT one of the gold paths just restored. Pass 1 alone only
       overwrites paths gold already has; liquid's gold tree has no
       conftest.py and no pytest.ini at all, so an agent-CREATED root
       conftest.py (e.g. a `pytest_report_teststatus` hook returning the
       literal string "passed" for everything, or `pytest_collection_
       modifyitems` skip-marking every item) previously survived pass 1
       entirely and scored solved=True with zero source change - demonstrated
       live, see task-4-report.md.

    Runs on the HOST (plain git/filesystem ops, no pytest needed) and BEFORE
    the container starts. Fetching task.fix_sha here is not a seal
    violation: the agent's run has already ended by the time scoring runs,
    so there is no further turn for a leaked "future" to reach.
    """
    remote = "membench-gold"
    clone_url = url or f"https://github.com/{task.repo}.git"  # url= override for tests only, mirrors workspace.provision's own shape
    subprocess.run(
        ["git", "remote", "add", remote, clone_url],
        cwd=workdir, check=True, capture_output=True,
    )
    try:
        subprocess.run(
            ["git", "fetch", "-q", "--depth", "1", remote, task.fix_sha],
            cwd=workdir, check=True, capture_output=True,
        )
        gold_paths = {
            p for p in subprocess.run(
                ["git", "ls-tree", "-r", "--name-only", "FETCH_HEAD"],
                cwd=workdir, capture_output=True, text=True, check=True,
            ).stdout.splitlines()
            if _looks_like_test_surface(p)
        }
        if gold_paths:
            # Restores content for paths that still exist AND recreates any
            # gold test file the agent deleted - `git checkout <ref> -- path`
            # accepts a path absent from the working tree as long as it's
            # present in the given ref's tree, which it is here by construction.
            subprocess.run(
                ["git", "checkout", "FETCH_HEAD", "--", *sorted(gold_paths)],
                cwd=workdir, check=True, capture_output=True,
            )
    finally:
        subprocess.run(["git", "remote", "remove", remote], cwd=workdir, check=False, capture_output=True)

    # `gold_paths` (superproject `git ls-tree -r`) never descends into a
    # submodule's own working tree - git treats a submodule as a single
    # "commit" entry, not expanded into its files. os.walk knows nothing
    # about that boundary, so without this exclusion the delete pass could
    # wipe legitimate content a submodule happens to name test_*.py/
    # conftest.py - same submodule-boundary precedent as workspace.py's own
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
            if _looks_like_test_surface(rel) and rel not in gold_paths:
                full.unlink()


def run_tests(
    workdir: Path, task: BenchTask, node_ids: list[str] | None = None, *, url: str | None = None
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
    """
    _reset_test_surface(workdir, task, url=url)

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

    proc = _docker_run(
        [
            *_CAP_ARGS,
            "-v", f"{workdir}:{_CONTAINER_WORKDIR}",
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
    # ponytail: does not clean the working tree's git-dirty state left by
    # `_reset_test_surface`'s checkout (`M tests/...`) - cosmetic (doesn't
    # execute anything, scoring already re-runs `_reset_test_surface` fresh
    # on every call), upgrade to `git checkout -- .` here first if a
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

    results = {t["nodeid"]: t["outcome"] in _PASSING_OUTCOMES for t in data.get("tests", [])}

    requested_exact = [n for n in (node_ids or []) if "::" in n]
    missing = [n for n in requested_exact if n not in results]
    if missing:
        raise RunTestsError(
            f"requested node id(s) not collectible in this environment (not present in "
            f"pytest's own report - see D31): {missing}"
        )
    return results
