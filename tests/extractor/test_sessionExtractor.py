import os
from pathlib import Path

from extractor.sessionExtractor import projectFiles


def _transcript(folder: Path, name: str, mtime: int | None = None) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{name}.jsonl"
    path.write_text('{"type":"user"}\n')
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def test_live_session_is_excluded_when_its_transcript_exists(tmp_path):
    """resume/compact/clear/fork all fire SessionStart on a session whose
    transcript already exists. Without the exclusion the session reads its
    own in-progress transcript back as a finished one."""
    folder = tmp_path / "proj"
    live = _transcript(folder, "live")
    other = _transcript(folder, "other")

    got = projectFiles(live)
    assert got == [other]
    assert live not in got


def test_nothing_is_dropped_when_the_live_transcript_does_not_exist(tmp_path):
    """`source: startup` - the hook fires before any transcript is written,
    so transcript_path names a file that is not there yet."""
    folder = tmp_path / "proj"
    a = _transcript(folder, "a")
    b = _transcript(folder, "b")
    live = folder / "never-written.jsonl"
    assert not live.exists()

    assert set(projectFiles(live)) == {a, b}


def test_oldest_first(tmp_path):
    folder = tmp_path / "proj"
    # Names deliberately sort OPPOSITE to mtime. With a/b the two orders
    # agree and the test passes whether the key is name or mtime.
    older = _transcript(folder, "zzz", mtime=1_000_000)
    newer = _transcript(folder, "aaa", mtime=2_000_000)
    live = _transcript(folder, "live", mtime=3_000_000)

    assert projectFiles(live) == [older, newer]


def test_only_jsonl_files_are_returned(tmp_path):
    folder = tmp_path / "proj"
    folder.mkdir(parents=True)
    keep = _transcript(folder, "keep")
    (folder / "notes.md").write_text("not a transcript")
    (folder / "subagents").mkdir()

    assert projectFiles(folder / "live.jsonl") == [keep]


def test_folder_with_only_the_live_session_returns_empty(tmp_path):
    folder = tmp_path / "proj"
    live = _transcript(folder, "live")
    assert projectFiles(live) == []


def test_missing_folder_returns_empty_rather_than_raising(tmp_path):
    """A project whose first session this is has no folder yet. The hook
    must not raise - an exception here breaks starting Claude."""
    assert projectFiles(tmp_path / "no-such-folder" / "live.jsonl") == []
