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
