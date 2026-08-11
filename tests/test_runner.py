import subprocess
from pathlib import Path

import pytest

from membench.corpus.extract import BenchTask
from membench.runner import RunTestsError, run_tests


def test_run_tests_reports_per_test_results(sample_task: BenchTask, provisioned_workdir: Path):
    results = run_tests(provisioned_workdir, sample_task, ["tests/test_cycle_tag.py"])
    assert results, "expected at least one test result"
    assert all(isinstance(v, bool) for v in results.values())


def test_run_tests_sets_utc(sample_task: BenchTask, provisioned_workdir: Path):
    # Date-filter tests fail outside UTC; a full run must still be green.
    results = run_tests(provisioned_workdir, sample_task, ["tests/filters/test_date.py", "tests/filters/test_datetime.py"])
    assert results, "expected at least one test result"
    assert sum(1 for v in results.values() if not v) == 0


def test_run_tests_raises_on_uncollectible_node_id(sample_task: BenchTask, provisioned_workdir: Path):
    # D31, C1 fix round: a REAL collection error, not an id that's merely
    # absent from an otherwise-collectible file (the previous version of
    # this test passed for the wrong reason - tests/test_large_str_to_int.py
    # collects fine and has six real tests; "test_something" was never one
    # of them). Plants a module with a genuinely bad import so the failure
    # mode under test actually exists.
    broken = provisioned_workdir / "tests" / "test_membench_broken_import_probe.py"
    broken.write_text("import totally_fake_module_membench_probe\n\n\ndef test_x():\n    pass\n")
    with pytest.raises(RunTestsError):
        run_tests(provisioned_workdir, sample_task, ["tests/test_membench_broken_import_probe.py::test_x"])


def test_run_tests_full_suite_continues_past_collection_errors(sample_task: BenchTask, provisioned_workdir: Path):
    # C1 fix round: the previous version of this test was vacuous - liquid's
    # 3 "residual" collection errors were an artifact of mock/hypothesis not
    # being in the image yet (fixed in Task 13); with them installed the
    # full suite has zero collection errors either way, so the flag made no
    # observable difference and the test passed regardless of its presence.
    # Plants a genuinely broken file so --continue-on-collection-errors has
    # something real to guard against: without it, this one broken file
    # would zero the ENTIRE report (verified manually, see task-4-report.md).
    broken = provisioned_workdir / "tests" / "test_membench_broken_import_probe2.py"
    broken.write_text("import another_fake_module_membench_probe\n")
    results = run_tests(provisioned_workdir, sample_task, None)
    assert len(results) > 3000, "one broken file must not blank the whole run"


def test_run_tests_restores_gold_test_files_defeats_rewritten_test(
    sample_task: BenchTask, provisioned_workdir: Path, liquid_repo: Path
):
    # D45(a): an agent that rewrites a failing test to `assert True` instead
    # of fixing the source must not score solved. issue 209's real fix commit
    # adds tests/test_issues.py::test_issue_209 - absent at base_sha. Plant a
    # fake, always-passing version at that exact name (source left untouched,
    # unfixed) and confirm run_tests reports it as failing anyway, because the
    # gold version (which actually exercises the bug) replaced it first.
    #
    # Reads base_sha content from `liquid_repo` (full history, session fixture
    # from conftest) rather than trusting the workdir's ambient file content -
    # by this point in the module, earlier tests have already called
    # run_tests, which unconditionally restores every test file to fix_sha
    # gold content as its first step. That's the feature under test, not a
    # bug, but it means the workdir's own test files no longer reflect
    # base_sha by the time this test runs.
    task = BenchTask(
        task_id=sample_task.task_id, repo=sample_task.repo, issue_number=sample_task.issue_number,
        issue_title="", issue_body="", base_sha=sample_task.base_sha, fix_sha=sample_task.fix_sha,
        changed_files=sample_task.changed_files, fail_to_pass=["tests/test_issues.py::test_issue_209"],
    )
    base_content = subprocess.run(
        ["git", "show", f"{sample_task.base_sha}:tests/test_issues.py"],
        cwd=liquid_repo, capture_output=True, text=True, check=True,
    ).stdout
    assert "test_issue_209" not in base_content, "fixture assumption: absent pre-fix"

    target = provisioned_workdir / "tests" / "test_issues.py"
    target.write_text(
        base_content + "\n\ndef test_issue_209() -> None:\n"
        "    assert True  # a cheating agent rewrote the test instead of fixing the source\n"
    )
    results = run_tests(provisioned_workdir, task, ["tests/test_issues.py::test_issue_209"])
    assert results["tests/test_issues.py::test_issue_209"] is False, (
        "the gold test (which exercises the real bug) must have replaced the "
        "agent's rewritten one and failed against the still-unfixed source"
    )
