"""Corpus backstop. If the known-correct patch does not score 100%, the harness
is broken and no agent number produced by it means anything.

A FALSE RED here is worse than a false green: it reads as "the gate is working"
while the real cause is a malformed corpus entry, so every failure path below
names its own cause instead of collapsing into `passed=False` (see GateError).
The same rule applies WITHIN a `passed=False`: `check_determinism` distinguishes
"the repeats disagreed" (the nondeterminism it exists to find) from "the repeats
agreed, on failure" (a golden-patch/baseline bug the other checks already name),
and carries the underlying check's own `detail` through either way.

D64 - the pre-agent git exception, written down because nothing else in the
codebase states it. `_apply_golden` runs `git apply` with `cwd=workdir`, which
looks like a violation of D57's categorical "no git command may touch the
agent-writable workspace" invariant (membench/runner.py:218-275). It is not: that
invariant is about SCORING a workspace an agent has already had write access to.
The gate's flow is provision -> apply -> score with NO agent step in between, so
the workspace `.git` is still entirely host-controlled - the same reason
`membench/workspace.py` runs git freely against `dest` during provisioning. The
invariant is post-agent, not universal. Anything that ever inserts an agent step
into a gate check must move this to a `git apply` from outside the workspace (or
a plain-filesystem write of gold blobs) before doing so.
"""

import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from membench.corpus.extract import BenchTask
from membench.runner import run_tests
from membench.scoring.correctness import score_correctness
from membench.workspace import provision


class GateError(RuntimeError):
    """D60: the corpus entry is malformed, which is NOT a gate result.

    Same shape and reasoning as `RunTestsError` (membench/runner.py:63) - raise
    instead of returning something indistinguishable from a real verdict.

    `CorrectnessScore.solved` requires `f2p_total > 0`
    (membench/scoring/correctness.py:19-26), so a task with an empty
    `fail_to_pass` scores `solved=False` on a repo where the golden patch
    demonstrably works. Returning `passed=False` for that would put "your task
    definition is broken" and "the golden patch did not fix the bug" on the same
    channel - exactly the confusion this gate exists to prevent.
    """


@dataclass
class GateResult:
    name: str
    passed: bool
    detail: str


@contextmanager
def _infra(task: BenchTask, doing: str):
    """Turns a raw `CalledProcessError` from the subprocess boundary into a
    named error a caller can classify.

    A bad `base_sha` (git fetch, rc 128, from inside `provision`), a
    `reference_repo` that is not a git clone, an unreachable repo slug - all
    three are INFRASTRUCTURE failures, not gate verdicts, and a bare
    subprocess exception escaping the gate says neither. Nothing is swallowed:
    the failing command, its exit code and its stderr all survive into the
    message, and the original exception stays chained.
    """
    try:
        yield
    except subprocess.CalledProcessError as e:
        err = e.stderr or b""
        if isinstance(err, bytes):
            err = err.decode(errors="replace")
        raise GateError(
            f"task {task.task_id!r}: {doing} FAILED - this is an infrastructure failure, not a "
            f"gate verdict. Command {e.cmd} exited {e.returncode}: {err.strip()}"
        ) from e


def _require_well_formed(task: BenchTask) -> None:
    if not task.fail_to_pass:
        raise GateError(
            f"task {task.task_id!r} has an empty fail_to_pass: there is nothing for the golden "
            f"patch to fix, so no gate verdict is meaningful. This is a corpus bug, not a failing "
            f"gate - populate fail_to_pass (see D60)."
        )
    if not task.changed_files:
        # Without a pathspec `git diff base fix --` emits the WHOLE diff, so an
        # empty list would silently widen the golden patch to every file the fix
        # commit touched instead of failing. Same class of corpus bug as above.
        raise GateError(
            f"task {task.task_id!r} has an empty changed_files: the golden patch would silently "
            f"widen to the fix commit's entire diff. This is a corpus bug, not a failing gate."
        )


def _apply_golden(task: BenchTask, workdir: Path, reference_repo: Path) -> None:
    """D63: the pathspec comes from `task.changed_files`, not a hardcoded
    `liquid/`, `tests/` - that literal is corpus-specific to one repo.

    `tests/` is absent from the pathspec, and that is harmless rather than
    guaranteed: `run_tests` unconditionally restores the entire test surface to
    `fix_sha` gold content as its first step (`_reset_test_surface`,
    membench/runner.py:218), so test paths in the pathspec would be overwritten
    anyway. The golden patch is the SOURCE fix only.

    Note this module does NOT enforce that exclusion. It holds today only
    because `extract.changed_files` filters on a corpus-specific default
    `prefix="liquid/"` (membench/corpus/extract.py:78-80). A caller passing a
    different prefix, or a repo whose source root is not `liquid/`, will put
    test paths into the pathspec - harmless per the above, but do not rely on
    this docstring as a guarantee.

    The diff is computed in `reference_repo` (trusted, full history); only the
    apply runs in `workdir` - see this module's docstring for why that is
    allowed here and nowhere post-agent.
    """
    with _infra(task, f"reading the golden diff from reference_repo {reference_repo}"):
        diff = subprocess.run(
            ["git", "diff", task.base_sha, task.fix_sha, "--", *task.changed_files],
            cwd=reference_repo, capture_output=True, text=True, check=True,
        )
    if not diff.stdout.strip():
        raise GateError(
            f"task {task.task_id!r}: the diff between base_sha and fix_sha over changed_files "
            f"{task.changed_files} is EMPTY - there is no golden patch to apply. Corpus bug."
        )
    applied = subprocess.run(
        ["git", "apply", "-"], cwd=workdir, input=diff.stdout, text=True, capture_output=True,
    )
    if applied.returncode != 0:
        # Named separately from a scoring failure: the patch not applying means
        # the workspace isn't at base_sha (or changed_files is wrong), which
        # would otherwise surface as an inexplicable "golden patch didn't fix it".
        raise GateError(
            f"task {task.task_id!r}: golden patch did not apply to the provisioned workspace "
            f"(rc={applied.returncode}): {applied.stderr.strip()}"
        )


def check_golden_patch(task: BenchTask, *, reference_repo: Path) -> GateResult:
    """Check 1: the known-correct patch must score 100%.

    `reference_repo` mirrors `run_tests` (membench/runner.py:336): required,
    keyword-only, no default (D62). Nothing in `membench/` creates a reference
    clone today - it exists only as a test fixture (tests/conftest.py:13-21) -
    so defaulting to a path here would hide that gap inside production code and
    let a caller silently forget it.
    """
    _require_well_formed(task)
    with tempfile.TemporaryDirectory() as td:
        with _infra(task, "provisioning the workspace"):
            wd = provision(task, Path(td) / "ws")
        _apply_golden(task, wd, reference_repo)
        with _infra(task, f"running the golden-patch tests (reference_repo {reference_repo})"):
            results = run_tests(
                wd, task, task.fail_to_pass + task.pass_to_pass, reference_repo=reference_repo
            )
        s = score_correctness(results, task)
        return GateResult(
            "golden_patch", s.solved,
            f"f2p {s.f2p_passed}/{s.f2p_total}, p2p broken {s.p2p_broken}, missing {s.missing}",
        )


def check_broken_baseline(task: BenchTask, *, reference_repo: Path) -> GateResult:
    """Check 2: the F2P targets must actually FAIL before the fix.

    An F2P id that already passes at `base_sha` measures nothing - an agent
    scores it by doing nothing at all.
    """
    _require_well_formed(task)
    with tempfile.TemporaryDirectory() as td:
        with _infra(task, "provisioning the workspace"):
            wd = provision(task, Path(td) / "ws")
        with _infra(task, f"running the baseline tests (reference_repo {reference_repo})"):
            results = run_tests(wd, task, task.fail_to_pass, reference_repo=reference_repo)
        already_passing = [n for n in task.fail_to_pass if results.get(n) is True]
        return GateResult(
            "broken_baseline", not already_passing,
            f"target tests already passing before any fix: {already_passing}",
        )


def check_determinism(task: BenchTask, n: int = 5, *, reference_repo: Path) -> GateResult:
    """Checks 3+4: both verdicts above must be stable across repeats.

    The property under test is AGREEMENT across repeats, so `n < 2` cannot
    establish it and is rejected (D60: raise, don't return). `n=0` would have
    been vacuously green (`all([]) is True`) and `n=1` green off a single
    sample - the two values a caller reaches for first.

    A `passed=False` here means one of two DIFFERENT things and says which:
    the repeats disagreed (real nondeterminism), or they agreed on failure
    (a golden-patch/baseline bug the other checks already name - reported as
    such rather than mislabelled "inconsistent", which was the exact inverse
    of the truth). Either way the failing sub-check's own `detail` is carried
    through, so a red result still states a reason.

    Cost is linear and not small - measured 4.9s (golden) + 4.7s (baseline) per
    repeat for liquid-209 on this machine, i.e. ~48s at the default n=5, and
    that is a small suite. Callers timing out should lower `n` toward 2 (never
    below), or drop the check for that task and say so, not soften the gate.
    """
    if n < 2:
        raise GateError(
            f"check_determinism needs n >= 2, got {n}: the property under test is agreement "
            f"ACROSS repeats, and n=0 (vacuously true) or n=1 (one sample) cannot show it. "
            f"This is a caller bug, not a gate verdict."
        )
    golden: list[GateResult] = []
    baseline: list[GateResult] = []
    for _ in range(n):
        golden.append(check_golden_patch(task, reference_repo=reference_repo))
        baseline.append(check_broken_baseline(task, reference_repo=reference_repo))

    for label, results in (("golden_patch", golden), ("broken_baseline", baseline)):
        if len({r.passed for r in results}) > 1:
            return GateResult(
                "determinism", False,
                f"NONDETERMINISM: {label} disagreed across {n} repeats "
                f"({sum(r.passed for r in results)}/{n} passed) - details per repeat: "
                f"{[(r.passed, r.detail) for r in results]}",
            )
    for first in (golden[0], baseline[0]):
        if not first.passed:
            return GateResult(
                "determinism", False,
                f"consistent across all {n} repeats, but {first.name} FAILED every time - this "
                f"is not a determinism problem, see the {first.name} check: {first.detail}",
            )
    return GateResult(
        "determinism", True,
        f"golden_patch and broken_baseline each agreed across all {n} repeats "
        f"(golden: {golden[0].detail}; baseline: {baseline[0].detail})",
    )
