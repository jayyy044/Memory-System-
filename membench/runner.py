import json
import subprocess
import uuid
from pathlib import Path

from membench.corpus.extract import BenchTask
from membench.driver import _CAP_ARGS, _CONTAINER_WORKDIR, _docker_run, _ensure_image

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


# C1/D44: pytest-json-report's per-test "outcome" is NOT limited to
# "passed"/"failed" - measured directly against liquid: "skipped" (a
# skipif'd compliance case) and "subtests passed" (pytest-subtests, all
# sub-checks green) are both real, non-failing outcomes that `== "passed"`
# silently scored as broken. That earlier version reported 14 non-passing
# ids on a run with ZERO actual failures and zero collection errors - a
# scorer bug reporting itself, not a repo defect (see task-4-report.md /
# D44). A denylist of the failure states is used rather than an allowlist of
# every non-failing string pytest-json-report might ever emit (xfailed,
# xpassed, ...) - "not an explicit failure" is the correct, forward-robust
# question for scoring, not "is it exactly this one spelling of pass".
_FAILING_OUTCOMES = {"failed", "error"}


def _looks_like_test_file(path: str) -> bool:
    name = Path(path).name
    return name == "conftest.py" or name.startswith("test_") or name.endswith("_test.py")


def _restore_gold_test_files(workdir: Path, task: BenchTask) -> None:
    """D45(a): test files live in the agent-writable /workspace bind mount
    and were never otherwise restored before scoring - an agent that
    rewrites its own failing test to `assert True` scored solved=True with
    zero source change. This is the standard SWE-bench mitigation
    (restore tests to the gold commit, leave source edits alone) and was
    simply absent. Runs on the HOST (plain git, not inside the sealed
    container - no pytest needed for this step) and BEFORE the container
    starts: fetches only task.fix_sha (not full history) from the upstream
    repo and checks out just the test-shaped paths present in it, so the
    agent's source edits stand and its test edits don't. Fetching a future
    commit here is not a seal violation - the agent's run has already ended
    by the time scoring runs; there is no further turn for a leaked "future"
    to reach.
    """
    remote = "membench-gold"
    subprocess.run(
        ["git", "remote", "add", remote, f"https://github.com/{task.repo}.git"],
        cwd=workdir, check=True, capture_output=True,
    )
    try:
        subprocess.run(
            ["git", "fetch", "-q", "--depth", "1", remote, task.fix_sha],
            cwd=workdir, check=True, capture_output=True,
        )
        paths = subprocess.run(
            ["git", "ls-tree", "-r", "--name-only", "FETCH_HEAD"],
            cwd=workdir, capture_output=True, text=True, check=True,
        ).stdout.splitlines()
        test_paths = [p for p in paths if _looks_like_test_file(p)]
        if test_paths:
            # Restores content for paths that still exist AND recreates any
            # gold test file the agent deleted - `git checkout <ref> -- path`
            # accepts a path absent from the working tree as long as it's
            # present in the given ref's tree, which it is here by construction.
            subprocess.run(
                ["git", "checkout", "FETCH_HEAD", "--", *test_paths],
                cwd=workdir, check=True, capture_output=True,
            )
    finally:
        subprocess.run(["git", "remote", "remove", remote], cwd=workdir, check=False, capture_output=True)


def run_tests(workdir: Path, task: BenchTask, node_ids: list[str] | None = None) -> dict[str, bool]:
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
    _restore_gold_test_files(workdir, task)

    image = _ensure_image()
    report_name = f".membench-report-{uuid.uuid4().hex[:8]}.json"
    report_host = workdir / report_name

    cmd = [
        "python3", "-m", "pytest", "-q",
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

    if not report_host.exists():
        raise RunTestsError(
            f"pytest produced no --json-report file (container rc={proc.returncode}); "
            f"stdout tail: {proc.stdout[-2000:]!r} stderr tail: {proc.stderr[-2000:]!r}"
        )
    try:
        data = json.loads(report_host.read_text())
    finally:
        report_host.unlink(missing_ok=True)

    results = {t["nodeid"]: t["outcome"] not in _FAILING_OUTCOMES for t in data.get("tests", [])}

    requested_exact = [n for n in (node_ids or []) if "::" in n]
    missing = [n for n in requested_exact if n not in results]
    if missing:
        raise RunTestsError(
            f"requested node id(s) not collectible in this environment (not present in "
            f"pytest's own report - see D31): {missing}"
        )
    return results
