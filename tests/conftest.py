from pathlib import Path
import subprocess

import pytest

from membench.corpus.extract import BenchTask, map_issue_to_commit, changed_files, base_sha

LIQUID = Path("fixtures/liquid")
ISSUE = 209


@pytest.fixture(scope="session")
def liquid_repo() -> Path:
    if not LIQUID.exists():
        LIQUID.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "clone", "-q", "https://github.com/jg-rp/liquid.git", str(LIQUID)],
            check=True,
        )
    return LIQUID


@pytest.fixture(scope="session")
def sample_task(liquid_repo: Path) -> BenchTask:
    fix_sha = map_issue_to_commit(liquid_repo, ISSUE)
    assert fix_sha is not None, f"issue #{ISSUE} must map to a commit; fixture is stale"
    return BenchTask(
        task_id=f"liquid-{ISSUE}",
        repo="jg-rp/liquid",
        issue_number=ISSUE,
        issue_title="",
        issue_body="",
        base_sha=base_sha(liquid_repo, fix_sha),
        fix_sha=fix_sha,
        changed_files=changed_files(liquid_repo, fix_sha),
    )
