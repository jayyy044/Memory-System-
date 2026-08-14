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

import math
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


# --- calibration checks ------------------------------------------------------
# Named for what they check, not for a plan check-number (D78): the plan's
# numbering collides with itself across tasks 8, 9 and 12.
#
# D74 (CORRECTED) - the input is per-arm, per-TASK-ID outcomes:
# `dict[str, dict[str, bool]]`, arm name -> task id -> `CorrectnessScore.solved`.
# Task 9's `run_benchmark` return type is this shape's contract.
#
# Two things get thrown away by the shapes this replaces, and each one produced
# a green result off data that could not support it:
#   - a pre-reduced float per arm (the plan's shape) hides the SAMPLE SIZE, and
#     a solve-rate difference is only interpretable against the sampling error
#     it could have come from;
#   - a bare `list[bool]` per arm (this module's first shape) hides TASK
#     IDENTITY. Null-vs-floor is defined as the same condition run twice, so
#     comparing it across two different task sets measures which tasks were
#     sampled, not the harness. Measured on the list shape:
#     `{"null": [True]*3, "floor": [True]*6 + [False]*6}` returned passed=True.

# CALIBRATION KNOB (D75), same status as MIN_SESSION_A_TOOL_CALLS
# (membench/session_a.py:64): a policy choice about false alarms, not a derived
# truth. Null and floor are the SAME condition run twice and agent runs are
# nondeterministic, so their solve rates differ by sampling noise; the plan's
# fixed tol=0.001 is far below that noise and would report a broken benchmark
# on every real run. 2.0 standard errors is ~95% two-sided, i.e. roughly a 1-in-22
# chance this gate cries wolf on a benchmark that is fine. Lower it to catch
# smaller real differences and accept more false reds; raise it and the check
# goes vacuous sooner (see _MAX_RATE_DIFF below).
SIGMA = 2.0

# A solve rate is in [0, 1], so no observed difference can ever exceed this. It
# is the yardstick the vacuity guard measures the tolerance against.
_MAX_RATE_DIFF = 1.0

# The oracle check's floor. A POLICY KNOB, unlike the null check's n=3, which is
# derived: there, n<=2 makes the tolerance meet _MAX_RATE_DIFF so the check
# provably cannot go red, and 3 is simply the first n where it can. Nothing
# equivalent forces 3 here - check_oracle_high can go red at n=1. It is set to 3
# because a green certifies the WHOLE corpus as agent-solvable and one task is
# not a corpus. 5 or 10 would be equally defensible; tune it against a real
# corpus run rather than treating this number as derived.
_MIN_ORACLE_TASKS = 3


def _outcomes(results: dict[str, dict[str, bool]], arm: str, check: str) -> dict[str, bool]:
    """The per-task-id outcomes for one arm, or `GateError`.

    NEVER a default. The plan's version used `.get(arm, 0.0)` on both arms,
    which made `check_null_equals_floor({})` - no runs at all - return
    `passed=True`: an empty experiment certifying itself. Verified against the
    plan's own code before this was written. An absent or empty arm is an
    orchestration bug and gets the D60 treatment (raise), not a verdict.
    """
    if arm not in results:
        raise GateError(
            f"{check}: no results for arm {arm!r} (got {sorted(results)}). This check compares "
            f"arms; with one missing there is nothing to compare and a 'pass' would mean the "
            f"benchmark never ran. Orchestration bug, not a gate verdict."
        )
    got = results[arm]
    if not isinstance(got, dict) or not all(
        isinstance(k, str) and isinstance(v, bool) for k, v in got.items()
    ):
        raise GateError(
            f"{check}: arm {arm!r} must be a dict[str, bool] mapping TASK ID to "
            f"CorrectnessScore.solved, one entry per task - got {type(got).__name__} {got!r}. "
            f"A pre-reduced float hides the sample size and a bare list[bool] hides task "
            f"identity; both calibration checks need each (D74 as corrected)."
        )
    if not got:
        raise GateError(
            f"{check}: arm {arm!r} has an empty result list - zero tasks were scored. A pass "
            f"here would certify an experiment that never ran."
        )
    return got


def _pooled_se(s1: int, n1: int, s2: int, n2: int) -> float:
    """Standard error of the difference of two proportions, pooled under the
    null hypothesis that both arms have the same true solve rate - which for
    null-vs-floor is not a hypothesis but the design."""
    p = (s1 + s2) / (n1 + n2)
    return math.sqrt(p * (1 - p) * (1 / n1 + 1 / n2))


def check_null_equals_floor(results: dict[str, dict[str, bool]]) -> GateResult:
    """The null arm and the floor arm are the same condition (see NullArm), so
    a difference between them is the harness's own run-to-run variance. If it
    exceeds sampling noise the benchmark is not reproducible and no arm
    comparison drawn from it means anything.

    The comparison is against `SIGMA` pooled standard errors, not a fixed
    tolerance (D75). READ THE POWER, NOT JUST THE VERDICT: a small sample cannot
    detect a small difference, so a pass at n=3 means very little - at n=3 per
    arm the smallest detectable difference is ~0.82, i.e. this check will only
    ever catch a near-total divergence. The detail line states that number for
    the sample actually supplied; an honest wide interval beats a precise wrong
    one.

    THIS CHECK CANNOT DETECT A DEAD BENCHMARK, AND MUST NOT BE READ ALONE.
    It measures reproducibility only. The all-same guard below raises at pooled
    rate exactly 0 or 1, but that closes one point of a continuum, not the
    continuum: measured at n=100 with floor solving 0, k=1 gives passed=True
    (diff=0.010, tolerance=0.020). A corpus solving one task in two hundred runs
    is genuinely reproducible, and this check is right to say so - detecting
    that nothing is solvable is `check_oracle_high`'s job. A green here is
    therefore only meaningful alongside a green oracle check; a caller that runs
    this one on its own can report a healthy benchmark that solves nothing.

    Both arms must cover EXACTLY the same task ids, and that is checked, not
    assumed: the two arms are the same condition run twice, so a difference
    computed across two different task sets measures which tasks were sampled
    rather than the harness's variance. It raises, because disjoint task sets
    are an orchestration bug and not an experimental outcome.

    Below that it stops being weak and becomes vacuous - at n<=2 per arm even
    total disagreement (one arm solves everything, the other nothing) sits
    inside SIGMA standard errors, so the check cannot go red at all. That raises
    rather than returning the green it structurally must return. `n<=2 per arm`
    is exact only because the task-set requirement above forces n1 == n2; for
    unequal arms the vacuous pairs are exactly {(1,1), (1,2), (1,3), (2,1),
    (2,2), (3,1)} - so n1=1 against n2=4 would NOT raise. Brute-forced over all
    (n1, n2) in [1,40]^2, not assumed, but unreachable here by construction.

    A third vacuity, and the worst of the three: if every outcome in both arms
    is identical - nothing solved anywhere, or everything solved - the pooled
    rate is 0 or 1, so the pooled SE is 0, the tolerance is 0, and diff=0 <= 0
    passes. A benchmark in which the agent never solves anything would report
    itself REPRODUCIBLE. Also raises; see the guard below.
    """
    null = _outcomes(results, "null", "check_null_equals_floor")
    floor = _outcomes(results, "floor", "check_null_equals_floor")
    if null.keys() != floor.keys():
        raise GateError(
            f"check_null_equals_floor: the two arms did not run the same tasks - "
            f"only in null: {sorted(null.keys() - floor.keys())}, "
            f"only in floor: {sorted(floor.keys() - null.keys())}. Null and floor are the SAME "
            f"condition run twice, so across different task sets the difference measures which "
            f"tasks were sampled, not the harness. Orchestration bug, not a gate verdict."
        )
    n1 = n2 = len(null)
    # Worst case = total disagreement: max possible diff (1.0), and the pooled
    # rate it implies. If even that passes, no result can fail this check.
    if SIGMA * _pooled_se(n1, n1, 0, n2) >= _MAX_RATE_DIFF:
        raise GateError(
            f"check_null_equals_floor: sample too small to mean anything - null n={n1}, "
            f"floor n={n2}. At this size even total disagreement (one arm solves every task, "
            f"the other none) is within {SIGMA} standard errors, so the check cannot fail and a "
            f"pass would certify nothing. Raise repeats or the task count; do not read this as "
            f"a green gate."
        )
    s1, s2 = sum(null.values()), sum(floor.values())
    if s1 + s2 in (0, n1 + n2):
        # Pooled rate 0 or 1 => pooled SE 0 => tolerance 0, and both arms are
        # necessarily identical, so this ALWAYS returned passed=True on data
        # carrying no information. Same class as the guard above (the check
        # cannot go red), so the same treatment - and the all-False case is the
        # one that matters: a dead benchmark must never read as reproducible.
        why = (
            "every task was solved by both arms: the corpus is too easy to measure memory with"
            if s1 else
            "NOTHING was solved by either arm: the harness or the corpus is broken, and this "
            "check would otherwise certify a dead benchmark as reproducible"
        )
        raise GateError(
            f"check_null_equals_floor: degenerate sample - {why} (null {s1}/{n1}, floor "
            f"{s2}/{n2}). The pooled rate is {'1' if s1 else '0'}, so the pooled standard error "
            f"is 0, the tolerance is 0, and the two rates carry no information: this check "
            f"cannot fail here and a pass means nothing. Do NOT read it as reproducibility "
            f"confirmed."
        )
    diff = abs(s1 / n1 - s2 / n2)
    tol = SIGMA * _pooled_se(s1, n1, s2, n2)
    return GateResult(
        "null_equals_floor", diff <= tol,
        f"null {s1}/{n1} (n={n1}) vs floor {s2}/{n2} (n={n2}): diff={diff:.3f}, "
        f"tolerance={tol:.3f} (SIGMA={SIGMA} pooled standard errors). A difference below "
        f"{tol:.3f} is undetectable at this sample size, so a pass is weak evidence.",
    )


def check_oracle_high(results: dict[str, dict[str, bool]], threshold: float = 0.8) -> GateResult:
    """The oracle arm is handed the golden answer's file list, so if it does not
    score high the task is not solvable by this agent at all and every other
    arm's number is measuring the model's ceiling rather than memory.

    Point estimate against `threshold`, with the sample size in the detail: the
    estimate is coarse at small n and stays honest by saying so rather than by
    widening. At n=3 the only rates available are 0, 0.33, 0.67 and 1.0, so the
    default threshold demands a perfect 3/3 - and 3/3 is still consistent with a
    true rate as low as ~0.37 (95% one-sided). The failure direction is the safe
    one: small samples make this check strict, not lenient.

    Strict is not the same as meaningful, though, which is why n is floored at
    `_MIN_ORACLE_TASKS`: a green here certifies that the whole corpus is
    agent-solvable, and one task is not a corpus. `threshold` is range-checked
    for the same reason - at threshold <= 0 the comparison is unconditionally
    true and 0/10 solved would certify the corpus.
    """
    if threshold <= 0.0 or threshold > 1.0:
        # NaN passes through this guard deliberately: every comparison against
        # NaN is False, so `rate >= threshold` below is False and a nonsense
        # threshold fails CLOSED. Raising would be safe too; failing closed is
        # the behaviour that already existed and is worth keeping.
        raise GateError(
            f"check_oracle_high: threshold={threshold} is outside (0, 1]. A solve rate is a "
            f"proportion, so at threshold <= 0 `rate >= threshold` is unconditionally true and "
            f"an oracle arm that solved NOTHING would certify the corpus as agent-solvable; "
            f"above 1 it can never pass. Caller bug, not a gate verdict."
        )
    oracle = _outcomes(results, "oracle", "check_oracle_high")
    n = len(oracle)
    solved = sum(oracle.values())
    rate = solved / n
    if n < _MIN_ORACLE_TASKS and rate >= threshold:
        # One-sided on purpose. The danger at small n is a GREEN read off too
        # little evidence - a pass here says every other arm's number measures
        # memory rather than the model's ceiling, and one task cannot support
        # that. A RED at small n is not the same thing: the oracle arm is handed
        # the answer, so failing to solve 1/1 or 0/2 is a real, informative
        # result and raising over it would swallow a finding. An earlier version
        # of this guard raised on both, which turned an accurate `passed=False`
        # into an exception.
        raise GateError(
            f"check_oracle_high: sample too small to certify a corpus - n={n}, minimum "
            f"{_MIN_ORACLE_TASKS}, and this input WOULD have passed ({solved}/{n} >= "
            f"{threshold}). At n<=2 the only rates that exist are 0, 0.5 and 1, so a single "
            f"task flipping moves the estimate by half the scale. Corpus/orchestration bug, "
            f"not a gate verdict. A FAILING oracle at this n is reported normally."
        )
    return GateResult(
        "oracle_high", rate >= threshold,
        f"oracle solved {solved}/{n} (n={n}) = {rate:.3f}, threshold {threshold}",
    )
