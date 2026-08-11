from pathlib import Path

import pytest

from membench.runner import RunTestsError, run_tests


def test_run_tests_reports_per_test_results(provisioned_workdir: Path):
    results = run_tests(provisioned_workdir, ["tests/test_cycle_tag.py"])
    assert results, "expected at least one test result"
    assert all(isinstance(v, bool) for v in results.values())


def test_run_tests_sets_utc(provisioned_workdir: Path):
    # Date-filter tests fail outside UTC; a full run must still be green.
    # D30 note: NOT a full-suite run - verified empirically (see
    # task-4-report.md) that fixtures/liquid at this base_sha has 14
    # genuinely non-passing node ids across the whole suite unrelated to TZ
    # (compliance/registered-filter tests, pre-existing at this commit,
    # reproducible with or without TZ=UTC). Asserting a fully-green *whole
    # suite* here would be testing liquid's repo state, not run_tests'
    # TZ handling. Scoped to the filter modules that actually exercise dates.
    results = run_tests(provisioned_workdir, ["tests/filters/test_date.py", "tests/filters/test_datetime.py"])
    assert results, "expected at least one test result"
    assert sum(1 for v in results.values() if not v) == 0


def test_run_tests_raises_on_uncollectible_node_id(provisioned_workdir: Path):
    # D31: liquid's suite has 3 residual collection errors in this
    # environment (mock, hypothesis - hatch dev-env only, not installed from
    # pyproject.toml's runtime deps). A requested F2P/P2P id landing in one
    # of those files must be surfaced loudly, not silently absent/failed.
    with pytest.raises(RunTestsError):
        run_tests(provisioned_workdir, ["tests/test_large_str_to_int.py::test_something"])


def test_run_tests_full_suite_continues_past_collection_errors(provisioned_workdir: Path):
    # Verified empirically: WITHOUT --continue-on-collection-errors, pytest
    # aborts the ENTIRE session on liquid's 3 residual collection errors and
    # runs zero tests - a full run silently returns {} for every task. This
    # guards the fix rather than the brief's original (empirically false)
    # "whole suite green" assumption.
    results = run_tests(provisioned_workdir, None)
    assert len(results) > 3000, "collection errors in 3 unrelated files must not blank the whole run"
