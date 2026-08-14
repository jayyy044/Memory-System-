import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from membench.corpus.extract import BenchTask
from membench.gate import checks
from membench.gate.checks import (
    GateError, GateResult, check_broken_baseline, check_determinism, check_golden_patch,
)


# D61: no F2P/P2P discovery exists (and Task 5 is not where it belongs), so the
# ids are hardcoded, following test_runner.py:49-54's precedent. Kept private to
# this module rather than promoted to conftest.py: test_runner.py already keeps
# its own private variant, and a shared fixture would couple two modules'
# notions of "the issue-209 task" together for no gain.
#
# P2P is populated too (test_runner.py's helper omits it): a gate that never
# exercises pass_to_pass is not checking regression at all. test_issue_202/203
# are in the same gold file as the F2P target, and test_runner.py's full-suite
# invariant (test_runner.py:57-73 - the ONLY non-passing node at base_sha is
# test_issue_209) is the evidence that they pass before the fix as well as after.
def _issue_209_gate_task(sample_task: BenchTask) -> BenchTask:
    return BenchTask(
        task_id=sample_task.task_id, repo=sample_task.repo, issue_number=sample_task.issue_number,
        issue_title="", issue_body="", base_sha=sample_task.base_sha, fix_sha=sample_task.fix_sha,
        changed_files=sample_task.changed_files,
        fail_to_pass=["tests/test_issues.py::test_issue_209"],
        pass_to_pass=[
            "tests/test_issues.py::test_issue_202",
            "tests/test_issues.py::test_issue_203",
        ],
    )


def test_task_without_f2p_targets_raises_rather_than_reporting_failure(
    sample_task: BenchTask, liquid_repo: Path
):
    # D60: `sample_task` carries fail_to_pass=[] (conftest.py:24-37) and
    # CorrectnessScore.solved requires f2p_total > 0 (correctness.py:19-26), so
    # a gate that just scored it would report passed=False on a repo where the
    # golden patch demonstrably works - a FALSE RED, read as "the gate caught
    # something" when the real cause is a malformed corpus entry. "your task is
    # malformed" and "the golden patch did not fix it" must not share a channel.
    with pytest.raises(GateError, match="fail_to_pass"):
        check_golden_patch(sample_task, reference_repo=liquid_repo)
    with pytest.raises(GateError, match="fail_to_pass"):
        check_broken_baseline(sample_task, reference_repo=liquid_repo)


def test_golden_patch_solves_task(sample_task: BenchTask, liquid_repo: Path):
    r = check_golden_patch(_issue_209_gate_task(sample_task), reference_repo=liquid_repo)
    assert r.passed, r.detail


def test_baseline_actually_fails(sample_task: BenchTask, liquid_repo: Path):
    r = check_broken_baseline(_issue_209_gate_task(sample_task), reference_repo=liquid_repo)
    assert r.passed, r.detail


def test_determinism_over_repeats(sample_task: BenchTask, liquid_repo: Path):
    # n=3, not the production default of 5, chosen from a measurement rather
    # than a guess: one check_golden_patch on this task timed at 4.9s and one
    # check_broken_baseline at 4.7s (provision + one container run each), so
    # check_determinism costs ~9.6s per repeat - 29s at n=3, 48s at n=5. n=3 is
    # cheap enough to keep in the default suite and is still >1, which is all
    # the property under test (agreement ACROSS repeats) actually requires.
    r = check_determinism(_issue_209_gate_task(sample_task), n=3, reference_repo=liquid_repo)
    assert r.passed, r.detail


# --- the red paths. Every guard in checks.py is killed by something below;
# without these the module's guards could all be deleted and the suite stayed
# green, which is the one thing a corpus backstop must not allow.


def test_task_without_changed_files_raises_rather_than_widening_the_patch(
    sample_task: BenchTask, liquid_repo: Path
):
    # An empty pathspec makes `git diff base fix --` emit the WHOLE fix diff,
    # so dropping this guard silently scores a patch nobody asked for.
    task = replace(_issue_209_gate_task(sample_task), changed_files=[])
    with pytest.raises(GateError, match="changed_files"):
        check_golden_patch(task, reference_repo=liquid_repo)


def test_empty_golden_diff_raises(sample_task: BenchTask, liquid_repo: Path):
    # changed_files that the fix commit never touched -> nothing to apply.
    # Unguarded, `git apply` of an empty patch fails with rc=128 "No valid
    # patches in input" (measured on git 2.55.0), so the guard is not what makes
    # this loud. What it buys is the right diagnosis: "the diff is EMPTY, the
    # corpus entry is wrong" instead of a generic apply failure that reads like
    # a broken patch. A git that accepts empty input would score green without
    # it.
    task = replace(_issue_209_gate_task(sample_task), changed_files=["no/such/path.py"])
    with pytest.raises(GateError, match="EMPTY"):
        check_golden_patch(task, reference_repo=liquid_repo)


def test_golden_patch_that_does_not_apply_raises(
    sample_task: BenchTask, liquid_repo: Path, monkeypatch: pytest.MonkeyPatch
):
    # Constructed by scrambling the workspace after provisioning: the patch's
    # context no longer matches, exactly as it would if the workspace were not
    # at base_sha. Without the guard this becomes "the golden patch didn't fix
    # it" - a red naming the wrong cause.
    real_provision = checks.provision

    def scrambling_provision(task: BenchTask, dest: Path, **kw):
        wd = real_provision(task, dest, **kw)
        for rel in task.changed_files:
            (wd / rel).write_text("not the base content\n")
        return wd

    monkeypatch.setattr(checks, "provision", scrambling_provision)
    with pytest.raises(GateError, match="did not apply"):
        check_golden_patch(_issue_209_gate_task(sample_task), reference_repo=liquid_repo)


def test_baseline_is_red_when_a_target_already_passes(
    sample_task: BenchTask, liquid_repo: Path
):
    # test_issue_202 passes at base_sha (test_runner.py:57-73's full-suite
    # invariant), so as an F2P target it measures nothing - an agent scores it
    # by doing nothing. That is the whole point of check 2.
    task = replace(
        _issue_209_gate_task(sample_task),
        fail_to_pass=["tests/test_issues.py::test_issue_202"],
        pass_to_pass=[],
    )
    r = check_broken_baseline(task, reference_repo=liquid_repo)
    assert not r.passed
    assert "test_issue_202" in r.detail


def test_infrastructure_failure_surfaces_as_a_named_gate_error(
    sample_task: BenchTask, liquid_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # A bad base_sha, an unreachable repo slug and a reference_repo that isn't
    # a git clone all used to escape as a bare CalledProcessError, which a
    # caller cannot tell apart from anything else. Both subprocess boundaries
    # are exercised; neither costs a clone or a container.
    task = _issue_209_gate_task(sample_task)

    def boom(t: BenchTask, dest: Path, **kw):
        raise subprocess.CalledProcessError(
            128, ["git", "fetch", "--depth", "1", "origin", t.base_sha], stderr=b"fatal: bad object"
        )

    monkeypatch.setattr(checks, "provision", boom)
    for check in (check_golden_patch, check_broken_baseline):
        with pytest.raises(GateError, match="infrastructure failure") as exc:
            check(task, reference_repo=liquid_repo)
        assert "git" in str(exc.value) and "128" in str(exc.value) and "bad object" in str(exc.value)

    monkeypatch.setattr(checks, "provision", lambda t, dest, **kw: tmp_path)
    with pytest.raises(GateError, match="reference_repo") as exc:
        check_golden_patch(task, reference_repo=tmp_path)
    assert "infrastructure failure" in str(exc.value)


def test_determinism_rejects_n_below_two(sample_task: BenchTask, liquid_repo: Path):
    # `all([])` is True, so n=0 was vacuously green; n=1 was green off one
    # sample. Both are caller bugs, and both were what the docstring's "lower
    # n" advice pointed at.
    for n in (0, 1):
        with pytest.raises(GateError, match="n >= 2"):
            check_determinism(_issue_209_gate_task(sample_task), n=n, reference_repo=liquid_repo)


def test_determinism_is_red_and_says_NONDETERMINISM_when_repeats_disagree(
    sample_task: BenchTask, liquid_repo: Path, monkeypatch: pytest.MonkeyPatch
):
    # Flapping injected at the run_tests boundary: the first golden repeat is
    # inverted, the second runs for real, so the two disagree. n=2 keeps this
    # to the minimum the property needs (~4 container runs).
    real_run_tests = checks.run_tests
    calls = iter(range(1_000))

    def flaky_run_tests(wd, task, node_ids=None, *, reference_repo):
        results = real_run_tests(wd, task, node_ids, reference_repo=reference_repo)
        return {k: not v for k, v in results.items()} if next(calls) == 0 else results

    monkeypatch.setattr(checks, "run_tests", flaky_run_tests)
    r = check_determinism(_issue_209_gate_task(sample_task), n=2, reference_repo=liquid_repo)
    assert not r.passed
    assert "NONDETERMINISM" in r.detail
    assert "golden_patch" in r.detail


def test_determinism_names_a_stable_failure_as_such_not_as_inconsistency(
    sample_task: BenchTask, liquid_repo: Path, monkeypatch: pytest.MonkeyPatch
):
    # The F1 inversion: five identical failures are perfectly CONSISTENT. The
    # old code reported "golden 0/5 consistent", the exact opposite, and threw
    # the sub-check's detail away. Monkeypatched at the sub-check boundary so
    # this costs nothing - the branch under test is the aggregation, not docker.
    monkeypatch.setattr(
        checks, "check_golden_patch",
        lambda task, *, reference_repo: GateResult("golden_patch", False, "f2p 0/1, p2p broken []"),
    )
    monkeypatch.setattr(
        checks, "check_broken_baseline",
        lambda task, *, reference_repo: GateResult("broken_baseline", True, ""),
    )
    r = check_determinism(_issue_209_gate_task(sample_task), n=5, reference_repo=liquid_repo)
    assert not r.passed
    assert "NONDETERMINISM" not in r.detail
    assert "consistent across all 5 repeats" in r.detail
    assert "golden_patch FAILED every time" in r.detail
    assert "f2p 0/1" in r.detail  # the sub-check's own reason survives


# --- calibration arms and their checks ---------------------------------------
# Arm tests live here rather than in tests/test_arms.py because the calibration
# arms exist only to serve the two checks below; they are one deliverable and
# splitting them across modules hides that a green arm test plus a green check
# test can still mean the pair measures nothing.

from membench.arms.calibration import (  # noqa: E402
    CalibrationArmError, NullArm, OracleArm, PathsOnlyArm,
)
from membench.arms.floor import FloorArm  # noqa: E402
from membench.gate.checks import (  # noqa: E402
    SIGMA, check_null_equals_floor, check_oracle_high,
)
from membench.models import ToolCall, Transcript  # noqa: E402


def _arm_task(**over) -> BenchTask:
    kw = dict(
        task_id="liquid-209", repo="jg-rp/liquid", issue_number=209,
        issue_title="cycle tag repeats", issue_body="The cycle tag repeats items.",
        base_sha="0" * 40, fix_sha="1" * 40, changed_files=["liquid/builtin/tags/cycle.py"],
    )
    kw.update(over)
    return BenchTask(**kw)


def _session_a() -> Transcript:
    """Touch order is DELIBERATELY not sorted order: loaders/base.py sorts after
    builtin/tags/cycle.py, so a `sorted(set(...))` in place of the arm's
    `dict.fromkeys(...)` reverses this list. With a sorted fixture that mutant
    survives every assertion, and touch order is the only structure PathsOnlyArm
    is allowed to carry."""
    return Transcript(
        text="I looked at the cycle tag and suspect the hash key.",
        tool_calls=[
            ToolCall(name="Read", file_path="liquid/loaders/base.py"),
            ToolCall(name="Bash", command="grep -rn cycle_hash liquid"),
            ToolCall(name="Edit", file_path="liquid/builtin/tags/cycle.py"),
            ToolCall(name="Read", file_path="liquid/builtin/tags/cycle.py"),
        ],
    )


# --- the arms ---

def test_null_arm_is_structurally_the_floor_arm(tmp_path: Path):
    # D76: the null arm IS floor under another name - the pair measures
    # harness run-to-run variance, so an independent `return ""` that a later
    # FloorArm edit leaves behind would make check_null_equals_floor fail for a
    # reason unrelated to what it tests. Identity, not equality of today's
    # output: a copy-pasted body passes the behavioural assertions below and
    # fails this one.
    assert NullArm.install is FloorArm.install
    wd = tmp_path / "ws"
    wd.mkdir()
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "a.md").write_text("session A tried X")
    assert NullArm().install(_arm_task(), wd, notes) == ""
    assert list(wd.iterdir()) == []
    assert NullArm().name != FloorArm().name  # they must still be two arms in the results


def test_oracle_arm_hands_over_the_changed_files(tmp_path: Path):
    wd = tmp_path / "ws"
    wd.mkdir()
    notes = tmp_path / "notes"
    notes.mkdir()
    task = _arm_task(changed_files=["liquid/a.py", "liquid/b.py"])
    preamble = OracleArm().install(task, wd, notes)
    assert "liquid/a.py" in preamble and "liquid/b.py" in preamble
    assert list(wd.iterdir()) == []


def test_oracle_arm_refuses_a_task_with_no_changed_files(tmp_path: Path):
    # An oracle that names no files is FloorArm with a misleading sentence, and
    # check_oracle_high would then go red as if memory were useless.
    with pytest.raises(CalibrationArmError, match="changed_files"):
        OracleArm().install(_arm_task(changed_files=[]), tmp_path, tmp_path)


def test_paths_only_arm_gives_the_paths_and_nothing_else(tmp_path: Path):
    # D77/DEBT-7: vault and ceiling both hand session B the touched-file list,
    # floor hands over nothing. This arm isolates that one confound - it must
    # carry the paths and NONE of the memory around them.
    wd = tmp_path / "ws"
    wd.mkdir()
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "a.md").write_text("session A tried the hash key and it failed")
    preamble = PathsOnlyArm(_session_a(), "liquid-209").install(_arm_task(), wd, notes)
    assert "liquid/builtin/tags/cycle.py" in preamble
    assert "liquid/loaders/base.py" in preamble
    assert preamble.count("liquid/builtin/tags/cycle.py") == 1  # touched twice, listed once
    # TOUCH order, not sorted order (see _session_a): the fixture touches
    # loaders/base.py first, which sorts LAST. `sorted(set(...))` in place of
    # `dict.fromkeys(...)` flips these two and nothing else in this file notices.
    assert preamble.index("liquid/loaders/base.py") < preamble.index(
        "liquid/builtin/tags/cycle.py"
    ), preamble
    for leak in ("hash key", "Read", "Edit", "grep -rn"):
        assert leak not in preamble, f"{leak!r} leaked: this is no longer a paths-only arm"
    assert list(wd.iterdir()) == []


def test_paths_only_arm_refuses_a_session_a_that_touched_no_files(tmp_path: Path):
    # Reachable: session_a.py's guards require tool calls, not file paths
    # (membench/session_a.py:178 renders "(none - it ran no file-path tool)").
    # An empty list here silently makes this arm identical to floor, and the
    # confound it exists to isolate would read as zero.
    bare = Transcript(text="", tool_calls=[ToolCall(name="Bash", command="ls")])
    with pytest.raises(CalibrationArmError, match="no file"):
        PathsOnlyArm(bare, "liquid-209").install(_arm_task(), tmp_path, tmp_path)


def test_paths_only_arm_refuses_a_transcript_from_another_task(tmp_path: Path):
    """`Transcript` carries no task id, so nothing in the transcript itself says
    which task it came from - the arm has to be told at construction. One
    instance reused across tasks would hand every session B the WRONG task's
    file hints and still look healthy: a full preamble, real paths, no error,
    and the file-path confound this arm exists to isolate measured against the
    wrong files."""
    with pytest.raises(CalibrationArmError, match="liquid-209"):
        PathsOnlyArm(_session_a(), "liquid-209").install(
            _arm_task(task_id="liquid-999"), tmp_path, tmp_path
        )


# --- the checks ---
# D74 (as CORRECTED): the input is per-arm, per-TASK-ID outcomes -
# `dict[str, dict[str, bool]]`, arm name -> task id -> `CorrectnessScore.solved`
# - not a pre-reduced float and not a bare `list[bool]`. The float hides the
# sample size; the list hides task identity, and null-vs-floor is DEFINED as the
# same condition run twice, so comparing it across two different task sets is
# meaningless and passed green under the list shape.

def _outcomes(solved: int, n: int, prefix: str = "t") -> dict[str, bool]:
    return {f"{prefix}{i}": i < solved for i in range(n)}


@pytest.mark.parametrize("bad", [
    {},                                              # no runs at all
    {"floor": _outcomes(2, 6)},                      # null missing
    {"null": _outcomes(2, 6)},                       # floor missing
    {"null": {}, "floor": _outcomes(2, 6)},          # null ran zero tasks
])
def test_null_check_raises_when_an_arm_is_absent_or_empty(bad):
    # The plan's version returns passed=True for {} - both .get defaults turn a
    # missing arm into 0.0, so zero runs certify the instrument. Verified
    # against the plan's own code: GateResult(passed=True, 'diff=0.000').
    with pytest.raises(GateError):
        check_null_equals_floor(bad)


def test_null_check_raises_when_BOTH_arms_are_empty():
    # The empty-arm guard's own test. The parametrized cases above reach a
    # DIFFERENT guard - one empty arm plus one populated arm trips the
    # same-task-set check first - so with a bare `pytest.raises(GateError)` they
    # passed for the wrong reason and the empty-arm guard survived mutation.
    # Both arms empty is the one input where it is the only thing standing.
    with pytest.raises(GateError, match="empty"):
        check_null_equals_floor({"null": {}, "floor": {}})


@pytest.mark.parametrize("bad", [
    {"null": 0.2, "floor": 0.2},                                  # the plan's float
    {"null": [True, False], "floor": [True, False]},              # the superseded list
    {"null": {"t0": 1}, "floor": {"t0": 1}},                      # ints, not bools
    {"null": {0: True}, "floor": {0: True}},                      # non-str task ids
])
def test_null_check_rejects_every_shape_but_task_id_to_bool(bad):
    with pytest.raises(GateError, match=r"dict\[str, bool\]"):
        check_null_equals_floor(bad)


def test_null_check_requires_both_arms_to_cover_the_same_tasks():
    """Null-vs-floor is the same condition run twice; across two different task
    sets the difference measures which tasks were sampled, not the harness's
    run-to-run variance. Under the superseded list shape
    `{"null": [True]*3, "floor": [True]*6 + [False]*6}` returned passed=True."""
    with pytest.raises(GateError, match="t3"):  # names the symmetric difference
        check_null_equals_floor({
            "null": _outcomes(3, 3),                     # t0 t1 t2
            "floor": _outcomes(6, 12),                   # t0 .. t11
        })
    with pytest.raises(GateError, match="a0"):  # fully disjoint
        check_null_equals_floor({
            "null": _outcomes(2, 6, prefix="a"),
            "floor": _outcomes(2, 6, prefix="b"),
        })


def test_null_matching_floor_passes():
    r = check_null_equals_floor({"null": _outcomes(2, 6), "floor": _outcomes(2, 6)})
    assert r.passed, r.detail
    assert r.name == "null_equals_floor"


def test_null_diverging_from_floor_fails():
    r = check_null_equals_floor({"null": _outcomes(6, 6), "floor": _outcomes(0, 6)})
    assert not r.passed, r.detail


def test_a_difference_inside_sampling_noise_is_not_a_failure():
    # D75: null and floor are the SAME condition run twice, and agent runs are
    # nondeterministic. 5/10 vs 6/10 is one task flipping; the plan's tol=0.001
    # would call that a broken benchmark on every real run.
    r = check_null_equals_floor({"null": _outcomes(5, 10), "floor": _outcomes(6, 10)})
    assert r.passed, r.detail
    assert abs(0.5 - 0.6) > 0.001  # the constant the plan would have used


def test_null_check_refuses_a_sample_too_small_to_ever_fail():
    # At n=2 per arm even total disagreement sits inside SIGMA standard errors,
    # so the check cannot go red - a green that means nothing. It must say so
    # rather than report a pass.
    with pytest.raises(GateError, match="too small"):
        check_null_equals_floor({"null": _outcomes(2, 2), "floor": _outcomes(0, 2)})
    # ...and the smallest sample it does accept can still fail.
    assert not check_null_equals_floor(
        {"null": _outcomes(3, 3), "floor": _outcomes(0, 3)}
    ).passed


def test_null_check_reports_the_sample_size_and_what_it_could_detect():
    r = check_null_equals_floor({"null": _outcomes(2, 6), "floor": _outcomes(2, 6)})
    assert "n=6" in r.detail and f"{SIGMA}" in r.detail


@pytest.mark.parametrize("solved", [0, 6], ids=["nothing-solved", "everything-solved"])
def test_null_check_refuses_a_benchmark_where_every_outcome_is_the_same(solved):
    """A DEAD benchmark certified green. With every outcome identical the pooled
    rate is 0 or 1, the pooled SE is 0, the tolerance is 0, and diff=0 <= 0
    reports a clean pass off two rates that carry no information at all: a
    harness that solves nothing reads as "reproducible". The check cannot go red
    here, which is the same vacuity the small-sample guard raises on, so it
    raises too."""
    with pytest.raises(GateError, match="no information"):
        check_null_equals_floor({"null": _outcomes(solved, 6), "floor": _outcomes(solved, 6)})


def test_null_check_passes_when_the_difference_exactly_equals_the_tolerance():
    """Pins `diff <= tol` against `diff < tol`. Both sides are EXACT in binary
    floating point here, which is why this case can be asserted at all:
    diff = 6/8 - 2/8 = 0.5, and tol = 2*sqrt(0.5*0.5*(1/8+1/8)) = 2*0.25 = 0.5.
    Verified: `d == t` is True, not merely close."""
    assert SIGMA == 2.0, "the exact boundary below is computed for SIGMA=2.0; recompute it"
    r = check_null_equals_floor({"null": _outcomes(6, 8), "floor": _outcomes(2, 8)})
    assert r.passed, r.detail
    assert "diff=0.500" in r.detail and "tolerance=0.500" in r.detail


@pytest.mark.parametrize("bad", [{}, {"oracle": {}}, {"floor": _outcomes(3, 3)}])
def test_oracle_check_raises_when_the_arm_is_absent_or_empty(bad):
    with pytest.raises(GateError):
        check_oracle_high(bad)


def test_oracle_check_reports_a_genuine_small_sample_failure_instead_of_raising():
    # The n<_MIN_ORACLE_TASKS guard is ONE-SIDED. The danger at small n is a
    # green read off too little evidence; a red is a real result - the oracle
    # arm is handed the answer, so failing to solve 1/1 is a finding, and an
    # earlier version of the guard raised over it, turning an accurate
    # passed=False into an exception.
    assert not check_oracle_high({"oracle": {"t0": False}}).passed
    assert not check_oracle_high({"oracle": {"t0": False, "t1": False}}).passed
    # ...while the would-be pass at the same n still raises.
    with pytest.raises(GateError, match="WOULD have passed"):
        check_oracle_high({"oracle": {"t0": True}})


@pytest.mark.parametrize("bad", [{"oracle": 0.9}, {"oracle": [True, True, True]}])
def test_oracle_check_rejects_every_shape_but_task_id_to_bool(bad):
    with pytest.raises(GateError, match=r"dict\[str, bool\]"):
        check_oracle_high(bad)


def test_oracle_must_score_high():
    assert check_oracle_high({"oracle": _outcomes(9, 10)}, threshold=0.8).passed
    assert not check_oracle_high({"oracle": _outcomes(3, 10)}, threshold=0.8).passed


def test_oracle_passes_when_the_rate_exactly_equals_the_threshold():
    # Pins `rate >= threshold` against `rate > threshold`. 8/10 == 0.8 exactly
    # in binary floating point (same double), so the boundary is assertable.
    assert check_oracle_high({"oracle": _outcomes(8, 10)}, threshold=0.8).passed


def test_oracle_check_refuses_a_sample_too_small_to_certify_a_corpus():
    """One solved task certifying that the whole corpus is agent-solvable is the
    dangerous direction of the null check's asymmetry: that one refuses n<=2
    loudly, this one accepted n=1 in silence and returned "oracle solved 1/1
    (n=1) = 1.000, passed". At n<=2 the only rates that exist are 0, 0.5 and 1,
    so one task flipping moves the estimate by half the scale."""
    for n in (1, 2):
        with pytest.raises(GateError, match="too small"):
            check_oracle_high({"oracle": _outcomes(n, n)})
    # The boundary, pinned: the smallest sample it accepts.
    assert check_oracle_high({"oracle": _outcomes(3, 3)}).passed


@pytest.mark.parametrize("bad", [0.0, -5.0, 1.5])
def test_oracle_check_rejects_a_threshold_outside_zero_to_one(bad):
    # threshold=0.0 (and anything below) makes `rate >= threshold` an
    # unconditional pass: 0/10 solved certified the corpus as agent-solvable.
    with pytest.raises(GateError, match="threshold"):
        check_oracle_high({"oracle": _outcomes(0, 10)}, threshold=bad)


def test_oracle_check_leaves_a_nan_threshold_failing_closed():
    # Every comparison against nan is False, so `rate >= nan` is False: a
    # nonsense threshold produces a red, not a green. The range guard above must
    # not turn that into an error - failing closed is the safe direction and is
    # preserved deliberately.
    assert not check_oracle_high({"oracle": _outcomes(10, 10)}, threshold=float("nan")).passed


def test_oracle_check_reports_the_sample_size():
    r = check_oracle_high({"oracle": _outcomes(9, 10)})
    assert "n=10" in r.detail
    assert r.name == "oracle_high"
