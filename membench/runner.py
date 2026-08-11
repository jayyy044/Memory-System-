import json
import uuid
from pathlib import Path

from membench.driver import _CAP_ARGS, _CONTAINER_WORKDIR, _docker_run, _ensure_image

# D30: pytest runs INSIDE the sealed container, not on the host. Two reasons:
# (a) liquid's dependencies are installed by seal-egress.sh from
# /workspace/pyproject.toml while the network is still open (D28) - a
# host-side `pytest` never sees them and reproduces the 113
# ModuleNotFoundError Task 13 fixed; (b) VALIDITY - scoring must run in the
# same environment the agent ran in, or a pass/fail verdict doesn't
# correspond to what the agent actually experienced. No network is needed
# for scoring, so this reuses run_agent's exact image/caps/entrypoint and
# lets the seal close all the way (no ANTHROPIC_HOST to allowlist here).
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
      (uncollectible - e.g. a module that fails to import). liquid's suite
      has 3 residual collection errors in this environment (mock, hypothesis
      - hatch dev-env only, not installed from pyproject.toml's runtime
      deps). A fail_to_pass/pass_to_pass id landing there must not silently
      score as "failed" for every arm; it must be surfaced as unusable.
    """


def run_tests(workdir: Path, node_ids: list[str] | None = None) -> dict[str, bool]:
    """Runs pytest for `workdir` inside the sealed container and returns
    {nodeid: passed}. `node_ids` may be exact node ids
    ("tests/x.py::test_y") or file/dir targets ("tests/x.py"); only exact
    node ids (containing "::") are checked for collectibility (D31) - a file
    target legitimately yields zero results if the whole file is skipped/
    filtered, which isn't the failure this guards against.
    """
    image = _ensure_image()
    report_name = f".membench-report-{uuid.uuid4().hex[:8]}.json"
    report_host = workdir / report_name

    cmd = [
        "python3", "-m", "pytest", "-q", "-p", "no:cacheprovider",
        # Verified empirically against liquid: WITHOUT this flag, pytest's
        # default behavior is to abort the entire session on ANY collection
        # error and run zero tests - not just skip the 3 broken files.
        # `--json-report-file` still gets written in that case, with
        # "tests": [] - so run_tests would silently return {} for the whole
        # suite, indistinguishable from "ran cleanly, nothing failed", for
        # every task, every arm. This flag makes collection errors local to
        # the file that has them (D31) instead of poisoning the whole run.
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

    results = {t["nodeid"]: t["outcome"] == "passed" for t in data.get("tests", [])}

    requested_exact = [n for n in (node_ids or []) if "::" in n]
    missing = [n for n in requested_exact if n not in results]
    if missing:
        raise RunTestsError(
            f"requested node id(s) not collectible in this environment (not present in "
            f"pytest's own report - see D31): {missing}"
        )
    return results
