import subprocess
from pathlib import Path

import pytest

from membench.corpus.extract import BenchTask
from membench.runner import RunTestsError, run_tests


def test_run_tests_reports_per_test_results(sample_task: BenchTask, provisioned_workdir: Path):
    results = run_tests(provisioned_workdir, sample_task, ["tests/test_cycle_tag.py"])
    assert results, "expected at least one test result"
    assert all(isinstance(v, bool) for v in results.values())


def _issue_209_task(sample_task: BenchTask) -> BenchTask:
    return BenchTask(
        task_id=sample_task.task_id, repo=sample_task.repo, issue_number=sample_task.issue_number,
        issue_title="", issue_body="", base_sha=sample_task.base_sha, fix_sha=sample_task.fix_sha,
        changed_files=sample_task.changed_files, fail_to_pass=["tests/test_issues.py::test_issue_209"],
    )


def test_run_tests_full_suite_only_failure_is_the_f2p_node(sample_task: BenchTask, provisioned_workdir: Path):
    # R3: the brief's original canary ("a full run must be green") does not
    # hold post gold-test-restoration - restoring tests/test_issues.py to
    # fix_sha content adds the real test_issue_209, which legitimately fails
    # against this task's still-unfixed base_sha source. That's not scorer
    # noise, it's the F2P mechanism working as intended (D48 corrects the
    # earlier "zero failures" ledger entry, which was true only pre-
    # restoration). The real invariant: a full run's ONLY non-passing node is
    # the task's own F2P id - nothing else regresses. TZ handling (why this
    # test originally existed) is exercised along the way by the date-filter
    # modules, which are part of the full run.
    task = _issue_209_task(sample_task)
    results = run_tests(provisioned_workdir, task, None)
    non_passing = [k for k, v in results.items() if not v]
    assert non_passing == task.fail_to_pass


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
    task = _issue_209_task(sample_task)
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


def test_planted_conftest_cannot_force_solved(sample_task: BenchTask, provisioned_workdir: Path):
    # R2/D50: liquid's gold tree has no conftest.py at any level, so nothing
    # in the old (restore-only-what-gold-has) mechanism ever touched an
    # agent-CREATED one. Demonstrated live in review: a root conftest.py
    # implementing pytest_report_teststatus to return the literal outcome
    # "passed" for every test defeats gold-test restoration AND an outcome
    # allowlist at once, scoring solved=True with the source left unfixed.
    task = _issue_209_task(sample_task)
    conftest = provisioned_workdir / "conftest.py"
    conftest.write_text(
        "def pytest_report_teststatus(report, config):\n"
        "    if report.when == 'call':\n"
        "        return 'passed', '.', 'PASSED'\n"
    )
    results = run_tests(provisioned_workdir, task, ["tests/test_issues.py::test_issue_209"])
    assert results["tests/test_issues.py::test_issue_209"] is False
    assert not conftest.exists(), "the planted conftest.py must be deleted, not merely outvoted"


@pytest.fixture()
def synthetic_repo(tmp_path_factory) -> tuple[Path, str, str]:
    """A minimal, fast, fully local git repo for exercising run_tests'
    collection-error paths without depending on liquid's real import graph -
    investigated and rejected: breaking any single liquid source module
    (e.g. cycle_tag.py, imported eagerly by liquid/builtin/__init__.py)
    cascades into breaking `import liquid` for the entire suite, not a
    narrow one-file collection error, so it can't isolate the behavior this
    is meant to test."""
    repo = tmp_path_factory.mktemp("gold-repo")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t.com"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True)
    (repo / "tests").mkdir()
    (repo / "tests" / "test_good.py").write_text("def test_good():\n    assert True\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "base"], check=True)
    base_sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()

    # The "gold"/fix commit ships a genuinely uncollectible test file - the
    # same shape liquid's own suite had pre-Task-13 (mock/hypothesis
    # missing), now deliberately constructed so it doesn't depend on an
    # external repo's dependency state.
    (repo / "tests" / "test_broken.py").write_text(
        "import totally_fake_module_membench_probe\n\n\ndef test_never_collected():\n    pass\n"
    )
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "fix"], check=True)
    fix_sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    return repo, base_sha, fix_sha


def _synthetic_workdir(repo: Path, base_sha: str, dest: Path) -> Path:
    # Not membench.workspace.provision(): that enforces --depth 1 shallow
    # history and fails loud on a local multi-commit source (M2, by design -
    # it's meant to catch exactly this). This fixture's workdir is never
    # agent-facing, only run_tests' own behavior is under test here, so a
    # plain clone + checkout is enough.
    subprocess.run(["git", "clone", "-q", str(repo), str(dest)], check=True)
    subprocess.run(["git", "-C", str(dest), "checkout", "-q", base_sha], check=True)
    subprocess.run(["git", "-C", str(dest), "remote", "remove", "origin"], check=True)
    return dest


def test_run_tests_raises_on_uncollectible_node_id(synthetic_repo, tmp_path: Path):
    # D31, R2 fix round: exercises a REAL collection error (bad import),
    # not an id merely absent from an otherwise-fine file.
    repo, base_sha, fix_sha = synthetic_repo
    task = BenchTask(
        task_id="synthetic", repo="local/synthetic", issue_number=0, issue_title="", issue_body="",
        base_sha=base_sha, fix_sha=fix_sha, changed_files=[],
    )
    wd = _synthetic_workdir(repo, base_sha, tmp_path / "ws")
    with pytest.raises(RunTestsError):
        run_tests(wd, task, ["tests/test_broken.py::test_never_collected"], url=str(repo))


def test_run_tests_full_suite_continues_past_collection_errors(synthetic_repo, tmp_path: Path):
    # R2 fix round: the previous version of this test planted a broken
    # TEST-shaped file, which _reset_test_surface now correctly deletes
    # before pytest ever sees it (that's the R2 fix working) - so it no
    # longer exercises --continue-on-collection-errors at all. Uses the gold
    # commit's own (deliberately broken) test_broken.py instead: restoration
    # copies it in like any other gold test file, so the collection error is
    # real and not defeated by the fix that landed one round later.
    repo, base_sha, fix_sha = synthetic_repo
    task = BenchTask(
        task_id="synthetic", repo="local/synthetic", issue_number=0, issue_title="", issue_body="",
        base_sha=base_sha, fix_sha=fix_sha, changed_files=[],
    )
    wd = _synthetic_workdir(repo, base_sha, tmp_path / "ws")
    results = run_tests(wd, task, None, url=str(repo))
    assert results.get("tests/test_good.py::test_good") is True, (
        "one broken gold test file must not blank the whole run"
    )
    assert "tests/test_broken.py::test_never_collected" not in results
