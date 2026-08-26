import json
import os
import re
from dataclasses import dataclass
from pathlib import Path


# Finding the claude directory for the transcripts
def claudeDir(transcriptDir=None) -> Path:
    """Where Claude Code keeps its data, including projects/<slug>/*.jsonl.

    Checked in order: an explicit argument (tests), CLAUDE_CONFIG_DIR
    (sealed/containerised runs), then the ~/.claude default.

    Returns the CONFIG root, not the projects folder - callers append
    "projects" themselves. All three branches must agree on what they
    return, or the same call site builds a different path depending on
    which branch fired.
    """
    if transcriptDir is not None:
        return Path(transcriptDir)

    fromEnv = os.environ.get("CLAUDE_CONFIG_DIR")
    if fromEnv:
        return Path(fromEnv)

    return Path.home() / ".claude"


def sessionSlug(path) -> str:
    """Claude Code's project-directory encoding: every character outside
    [A-Za-z0-9-] becomes '-'.

    Verified against all 60 project directories on this machine (60/60) by
    re-encoding the `cwd` recorded inside each one's first transcript. A
    narrower rule replacing only '/', '.' and '_' scored 59/60 - it missed
    a path containing a space.

    LOSSY: '/a/b c', '/a/b.c' and '/a/b-c' all produce the same slug, so a
    directory name is never proof of provenance. Confirm `cwd` from inside
    the file before trusting a transcript is yours.
    """
    return re.sub(r"[^A-Za-z0-9-]", "-", str(path))


def sessionCwd(transcript) -> str | None:
    """The `cwd` recorded inside a transcript, or None if it has none.

    Stops at the first hit. Transcripts reach 5 MB in this repo alone and
    the answer is almost always on line 1, so reading one whole to check
    ownership would cost thousands of times what the question needs.
    """
    try:
        with open(transcript, errors="replace") as handle:
            for line in handle:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue  # transcripts carry occasional non-JSON lines
                cwd = event.get("cwd")
                if cwd:
                    return cwd
    except OSError:
        return None
    return None


def sessionFiles(repo, transcriptDir=None, liveSession=None) -> list[Path]:
    """Transcripts belonging to `repo`, oldest first.

    `liveSession` drops one session id - pass the running session's own,
    whose transcript is still being appended to and is not yet a record of
    anything finished.
    """
    repo = str(Path(repo).resolve())
    folder = claudeDir(transcriptDir) / "projects" / sessionSlug(repo)
    if not folder.is_dir():
        return []

    found = []
    for path in folder.glob("*.jsonl"):
        if liveSession and path.stem == liveSession:
            continue
        # sessionSlug is lossy, so this folder can legitimately hold another
        # repo's sessions. Inheriting a different project's dead ends is
        # worse than inheriting none, so confirm rather than assume.
        if sessionCwd(path) != repo:
            continue
        found.append(path)

    return sorted(found, key=lambda p: p.stat().st_mtime)


# Which input key carries the meaningful target, tried in order. Derived from
# every tool actually used across 8 real sessions: Bash(429) command,
# Agent(66) description, Edit(48)/Write(24)/Read(31) file_path, Skill(10)
# skill, ToolSearch(6)/WebSearch(4) query, WebFetch(3) url. Grep and Glob
# never appeared - this setup routes searches through Bash - so there is no
# `pattern` entry until a transcript actually shows one.
_TARGET_KEYS = ("command", "file_path", "path", "url", "skill", "query", "description")

_RESULT_EXCERPT_CHARS = 200


@dataclass
class Call:
    """One tool call and how it turned out.

    `ok` is `tool_result.is_error` inverted, and it is NOT "the approach
    worked". Measured across 8 sessions, 9 of 16 errors were commands that
    never executed: blocked by a pre-flight guard ("Brace expansion",
    "Contains shell syntax that cannot be statically..."), refused by the
    user ("The user doesn't want to proceed"), or over a size limit. Only
    "Exit code N" errors are the agent's approach failing. Classifying that
    is the detector's job - this keeps `result` so it can.

    `ok is None` means no result was ever recorded: the session stopped
    between the call and its answer.
    """

    name: str
    target: str | None
    ok: bool | None
    result: str
    turn: int


def sessionCalls(transcript) -> list[Call]:
    """Every tool call in one transcript, in order.

    A call and its result live in DIFFERENT events - the call in an
    `assistant` event, the result in a later `user` event - joined by
    `tool_use_id`. So this is a two-pass join, not a linear scan: results
    are collected first because a call's answer always appears after it.
    """
    calls: list[tuple[str, str, str | None]] = []  # (id, name, target)
    results: dict[str, tuple[bool, str]] = {}

    with open(transcript, errors="replace") as handle:
        for line in handle:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            content = event.get("message", {}).get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict):
                    continue
                kind = block.get("type")
                if kind == "tool_use":
                    payload = block.get("input") or {}
                    target = None
                    for key in _TARGET_KEYS:
                        value = payload.get(key)
                        if isinstance(value, str) and value.strip():
                            target = " ".join(value.split())
                            break
                    calls.append((block.get("id", ""), block.get("name", ""), target))
                elif kind == "tool_result":
                    results[block.get("tool_use_id", "")] = (
                        not block.get("is_error", False),
                        " ".join(str(block.get("content", "")).split())[:_RESULT_EXCERPT_CHARS],
                    )

    out = []
    for turn, (callId, name, target) in enumerate(calls):
        found = results.get(callId)
        out.append(
            Call(
                name=name,
                target=target,
                ok=found[0] if found else None,
                result=found[1] if found else "",
                turn=turn,
            )
        )
    return out
