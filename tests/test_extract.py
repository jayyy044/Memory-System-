from pathlib import Path
import subprocess
import pytest
from membench.corpus.extract import map_issue_to_commit, changed_files, AmbiguousMapping

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


def _commit(path: Path, filename: str, content: str, message: str) -> None:
    (path / filename).write_text(content)
    subprocess.run(["git", "-C", str(path), "add", filename], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-q", "-m", message], check=True)


def test_ambiguous_mapping_raises(tmp_path: Path):
    """Two commits both claim to close the same issue (fix + later regression
    fix) — the function must surface this rather than silently pick the
    newest one."""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "t@t.com"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "t"], check=True)
    _commit(tmp_path, "a.txt", "1", "fixes #7 first attempt")
    _commit(tmp_path, "a.txt", "2", "fixes #7 for real this time")
    with pytest.raises(AmbiguousMapping):
        map_issue_to_commit(tmp_path, 7)
