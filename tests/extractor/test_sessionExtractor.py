import json
import os
from pathlib import Path

from extractor.sessionExtractor import (
    claudeDir,
    sessionCwd,
    sessionFiles,
    sessionSlug,
)


def _transcript(path: Path, cwd: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"type": "user", "cwd": cwd}) + "\n")
    return path


def test_slug_replaces_every_non_alphanumeric():
    assert sessionSlug("/Users/x/Projects/my_repo") == "-Users-x-Projects-my-repo"
    assert sessionSlug("/Users/x/Downloads/Exam 2") == "-Users-x-Downloads-Exam-2"
    assert sessionSlug("/tmp/cfgtest.9VZD/work") == "-tmp-cfgtest-9VZD-work"


def test_slug_is_lossy_which_is_why_cwd_gets_verified():
    assert sessionSlug("/a/b c") == sessionSlug("/a/b-c") == sessionSlug("/a/b.c")


def test_claude_dir_precedence(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "env"))
    assert claudeDir(tmp_path / "explicit") == tmp_path / "explicit"
    assert claudeDir() == tmp_path / "env"
    monkeypatch.delenv("CLAUDE_CONFIG_DIR")
    assert claudeDir() == Path.home() / ".claude"


def test_all_three_branches_return_the_same_kind_of_root(tmp_path, monkeypatch):
    """Every branch must return the CONFIG root. When one appended
    'projects' and the others did not, the same call site produced
    <root>/projects/projects/<slug> only when the env var was unset."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    assert claudeDir().name != "projects"
    monkeypatch.delenv("CLAUDE_CONFIG_DIR")
    assert claudeDir().name != "projects"
    assert claudeDir(tmp_path).name != "projects"


def test_finds_transcripts_oldest_first(tmp_path):
    repo = tmp_path / "repo"; repo.mkdir()
    cfg = tmp_path / "cfg"
    folder = cfg / "projects" / sessionSlug(str(repo))
    # Names deliberately sort OPPOSITE to mtime. With aaa/bbb the two orders
    # agree and the test passes whether the sort key is name or mtime.
    older = _transcript(folder / "zzz.jsonl", str(repo))
    newer = _transcript(folder / "aaa.jsonl", str(repo))
    os.utime(older, (1_000_000, 1_000_000))
    os.utime(newer, (2_000_000, 2_000_000))
    assert sessionFiles(repo, transcriptDir=cfg) == [older, newer]


def test_live_session_is_dropped_and_nothing_else_is(tmp_path):
    repo = tmp_path / "repo"; repo.mkdir()
    cfg = tmp_path / "cfg"
    folder = cfg / "projects" / sessionSlug(str(repo))
    live = _transcript(folder / "live-id.jsonl", str(repo))
    done = _transcript(folder / "done-id.jsonl", str(repo))
    # Falsifiable: without the flag BOTH appear, so a no-op skip fails here.
    assert set(sessionFiles(repo, transcriptDir=cfg)) == {live, done}
    assert sessionFiles(repo, transcriptDir=cfg, liveSession="live-id") == [done]


def test_foreign_transcript_sharing_a_slug_is_rejected(tmp_path):
    """The collision the lossy slug makes possible. '/x/my repo' and
    '/x/my-repo' encode to the same folder; serving one project's dead
    ends to the other is worse than serving none."""
    mine = tmp_path / "my-repo"; mine.mkdir()
    theirs = tmp_path / "my repo"; theirs.mkdir()
    assert sessionSlug(str(mine)) == sessionSlug(str(theirs))  # collision is real

    cfg = tmp_path / "cfg"
    folder = cfg / "projects" / sessionSlug(str(mine))
    ours = _transcript(folder / "ours.jsonl", str(mine))
    alien = _transcript(folder / "alien.jsonl", str(theirs))

    got = sessionFiles(mine, transcriptDir=cfg)
    assert got == [ours]
    assert alien not in got


def test_trailing_slash_and_relative_paths_resolve(tmp_path, monkeypatch):
    repo = tmp_path / "repo"; repo.mkdir()
    cfg = tmp_path / "cfg"
    folder = cfg / "projects" / sessionSlug(str(repo.resolve()))
    t = _transcript(folder / "a.jsonl", str(repo.resolve()))
    assert sessionFiles(str(repo) + "/", transcriptDir=cfg) == [t]
    monkeypatch.chdir(repo)
    assert sessionFiles(".", transcriptDir=cfg) == [t]


def test_missing_folder_returns_empty(tmp_path):
    assert sessionFiles(tmp_path / "nope", transcriptDir=tmp_path / "cfg") == []


def test_session_cwd_survives_junk(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_text('not json\n{"type":"x"}\n{"cwd":"/repo"}\n')
    assert sessionCwd(p) == "/repo"
    assert sessionCwd(tmp_path / "absent.jsonl") is None
    (tmp_path / "e.jsonl").write_text("")
    assert sessionCwd(tmp_path / "e.jsonl") is None


# --- sessionCalls ---------------------------------------------------------

def _events(*events) -> str:
    return "\n".join(json.dumps(e) for e in events) + "\n"


def _use(id_, name, **inputs):
    return {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": id_, "name": name, "input": inputs}]}}


def _res(id_, content, is_error=False):
    return {"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": id_, "content": content,
         "is_error": is_error}]}}


def test_call_and_result_join_across_separate_events(tmp_path):
    from extractor.sessionExtractor import sessionCalls
    t = tmp_path / "t.jsonl"
    # Deliberately interleaved and out of adjacency: b's result arrives before
    # a's. A positional pairing would swap them; the id join must not.
    t.write_text(_events(
        _use("a", "Bash", command="pytest -k parser"),
        _use("b", "Read", file_path="/repo/x.py"),
        _res("b", "file contents"),
        _res("a", "Exit code 1", is_error=True),
    ))
    calls = sessionCalls(t)
    byName = {c.name: c for c in calls}
    assert byName["Bash"].target == "pytest -k parser"
    assert byName["Bash"].ok is False
    assert byName["Bash"].result == "Exit code 1"
    assert byName["Read"].target == "/repo/x.py"
    assert byName["Read"].ok is True


def test_call_with_no_result_is_none_not_false(tmp_path):
    """A session that stopped between a call and its answer. `None` and
    `False` mean different things: unknown vs failed."""
    from extractor.sessionExtractor import sessionCalls
    t = tmp_path / "t.jsonl"
    t.write_text(_events(_use("a", "Bash", command="sleep 999")))
    (call,) = sessionCalls(t)
    assert call.ok is None
    assert call.ok is not False


def test_target_comes_from_the_right_key_per_tool(tmp_path):
    from extractor.sessionExtractor import sessionCalls
    t = tmp_path / "t.jsonl"
    t.write_text(_events(
        _use("1", "Bash", command="ls", description="listing"),
        _use("2", "Read", file_path="/a.py"),
        _use("3", "WebFetch", url="https://x.test", prompt="ignored"),
        _use("4", "Skill", skill="brainstorming"),
        _use("5", "Agent", description="scout the repo", prompt="long..."),
    ))
    assert [c.target for c in sessionCalls(t)] == [
        "ls", "/a.py", "https://x.test", "brainstorming", "scout the repo"]


def test_command_wins_over_description_for_bash(tmp_path):
    """Bash carries BOTH command(429) and description(429) in real
    transcripts. The command is the action; the description is prose."""
    from extractor.sessionExtractor import sessionCalls
    t = tmp_path / "t.jsonl"
    t.write_text(_events(_use("1", "Bash", description="run the tests", command="pytest")))
    assert sessionCalls(t)[0].target == "pytest"


def test_order_is_preserved_and_turn_increments(tmp_path):
    from extractor.sessionExtractor import sessionCalls
    t = tmp_path / "t.jsonl"
    t.write_text(_events(
        _use("1", "Bash", command="first"),
        _use("2", "Bash", command="second"),
        _use("3", "Bash", command="third"),
    ))
    calls = sessionCalls(t)
    assert [c.target for c in calls] == ["first", "second", "third"]
    assert [c.turn for c in calls] == [0, 1, 2]


def test_result_is_truncated_and_whitespace_collapsed(tmp_path):
    from extractor.sessionExtractor import sessionCalls
    t = tmp_path / "t.jsonl"
    t.write_text(_events(_use("a", "Bash", command="x"), _res("a", "A" * 500)))
    (call,) = sessionCalls(t)
    assert len(call.result) == 200

    t.write_text(_events(_use("b", "Bash", command="x"), _res("b", "line1\n\n  line2")))
    assert sessionCalls(t)[0].result == "line1 line2"


def test_blocked_and_failed_are_both_ok_false_but_distinguishable(tmp_path):
    """The reader must not classify - it must preserve enough that the
    detector can. 9 of 16 real errors never executed."""
    from extractor.sessionExtractor import sessionCalls
    t = tmp_path / "t.jsonl"
    t.write_text(_events(
        _use("a", "Bash", command="x"), _res("a", "Exit code 1", is_error=True),
        _use("b", "Bash", command="y"), _res("b", "Brace expansion", is_error=True),
    ))
    a, b = sessionCalls(t)
    assert a.ok is False and b.ok is False
    assert a.result.startswith("Exit code")
    assert not b.result.startswith("Exit code")


def test_junk_and_non_list_content_are_skipped(tmp_path):
    from extractor.sessionExtractor import sessionCalls
    t = tmp_path / "t.jsonl"
    t.write_text(
        "not json\n"
        + json.dumps({"type": "user", "message": {"content": "a plain string"}}) + "\n"
        + json.dumps({"type": "file-history-snapshot"}) + "\n"
        + _events(_use("a", "Bash", command="survivor"))
    )
    assert [c.target for c in sessionCalls(t)] == ["survivor"]
