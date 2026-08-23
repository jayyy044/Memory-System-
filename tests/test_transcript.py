import json
from pathlib import Path

import pytest

from handoff.transcript import config_root, session_cwd, slug, transcripts_for


def _write(path: Path, cwd: str, extra: dict | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    events = [{"type": "user", "cwd": cwd, **(extra or {})}]
    path.write_text("\n".join(json.dumps(e) for e in events))
    return path


def test_slug_replaces_every_non_alphanumeric():
    assert slug("/Users/x/Projects/my_repo") == "-Users-x-Projects-my-repo"
    assert slug("/Users/x/Downloads/Exam 2") == "-Users-x-Downloads-Exam-2"
    assert slug("/tmp/cfgtest.9VZD/work") == "-tmp-cfgtest-9VZD-work"


def test_slug_is_lossy_which_is_why_cwd_is_verified():
    # Not a wish - a property. Two different repos share one directory.
    assert slug("/a/b c") == slug("/a/b-c") == slug("/a/b.c")


def test_config_root_prefers_explicit_then_env(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "from_env"))
    assert config_root(tmp_path / "explicit") == tmp_path / "explicit"
    assert config_root() == tmp_path / "from_env"
    monkeypatch.delenv("CLAUDE_CONFIG_DIR")
    assert config_root() == Path.home() / ".claude"


def test_finds_transcripts_for_a_repo_oldest_first(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = tmp_path / "cfg"
    d = cfg / "projects" / slug(str(repo))
    older = _write(d / "aaa.jsonl", str(repo))
    newer = _write(d / "bbb.jsonl", str(repo))
    import os
    os.utime(older, (1_000_000, 1_000_000))
    os.utime(newer, (2_000_000, 2_000_000))

    assert transcripts_for(repo, config_dir=cfg) == [older, newer]


def test_excluded_session_is_dropped_and_only_that_one(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = tmp_path / "cfg"
    d = cfg / "projects" / slug(str(repo))
    live = _write(d / "live-id.jsonl", str(repo))
    done = _write(d / "done-id.jsonl", str(repo))

    # Falsifiable: without the flag BOTH are found. A no-op exclude fails here.
    assert set(transcripts_for(repo, config_dir=cfg)) == {live, done}
    assert transcripts_for(repo, config_dir=cfg, exclude_session="live-id") == [done]


def test_foreign_transcript_in_a_colliding_directory_is_rejected(tmp_path):
    """The slug is lossy, so `/x/my repo` and `/x/my-repo` share a directory.
    Serving one project's dead ends to the other is worse than serving none."""
    mine = tmp_path / "my-repo"
    mine.mkdir()
    cfg = tmp_path / "cfg"
    d = cfg / "projects" / slug(str(mine))
    ours = _write(d / "ours.jsonl", str(mine))
    theirs = _write(d / "theirs.jsonl", str(tmp_path / "my repo"))

    assert slug(str(tmp_path / "my repo")) == d.name  # the collision is real
    assert transcripts_for(mine, config_dir=cfg) == [ours]
    assert theirs not in transcripts_for(mine, config_dir=cfg)


def test_missing_directory_returns_empty(tmp_path):
    assert transcripts_for(tmp_path / "nope", config_dir=tmp_path / "cfg") == []


def test_session_cwd_handles_junk_lines_and_unreadable_files(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_text('not json\n{"type":"x"}\n{"cwd":"/repo"}\n')
    assert session_cwd(p) == "/repo"
    assert session_cwd(tmp_path / "absent.jsonl") is None
    (tmp_path / "empty.jsonl").write_text("")
    assert session_cwd(tmp_path / "empty.jsonl") is None
