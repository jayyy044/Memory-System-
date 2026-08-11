from pathlib import Path
import subprocess
import pytest
from membench.corpus.extract import map_issue_to_commit, changed_files

LIQUID = Path("fixtures/liquid")


@pytest.fixture(scope="module")
def repo() -> Path:
    if not LIQUID.exists():
        LIQUID.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "clone", "-q", "https://github.com/jg-rp/liquid.git", str(LIQUID)],
            check=True,
        )
    return LIQUID


def test_maps_fix_branch_issue(repo: Path):
    sha = map_issue_to_commit(repo, 209)
    assert sha is not None
    files = changed_files(repo, sha)
    assert "liquid/builtin/expressions/loop.py" in files


def test_maps_changelog_pickaxe_issue(repo: Path):
    sha = map_issue_to_commit(repo, 202)
    assert sha is not None
    files = changed_files(repo, sha)
    assert "liquid/builtin/tags/cycle_tag.py" in files


def test_unmappable_issue_returns_none(repo: Path):
    assert map_issue_to_commit(repo, 999999) is None
