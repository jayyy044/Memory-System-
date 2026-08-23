"""Locate Claude Code session transcripts on disk.

Claude Code writes one JSONL file per session under
`$CLAUDE_CONFIG_DIR/projects/<slug>/<session-id>.jsonl`, appending as the
session runs. That means a transcript is complete even when the session died
without a clean exit - the case a handoff record most needs to cover.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

_SLUG_RE = re.compile(r"[^A-Za-z0-9-]")


def slug(path: str | os.PathLike) -> str:
    """Claude Code's project-directory encoding: every character outside
    `[A-Za-z0-9-]` becomes `-`.

    Verified 2026-08-22 against all 60 project directories on this machine
    (60/60 exact match) by reading each directory's first transcript and
    re-encoding the `cwd` recorded inside it. A narrower rule replacing only
    `/`, `.` and `_` matched 59/60 - it missed a path containing a space.

    The encoding is LOSSY: `/a/b c`, `/a/b.c` and `/a/b-c` all produce the
    same slug. Never treat a directory name as proof of provenance; confirm
    `cwd` from inside the file. `transcripts_for` does this.
    """
    return _SLUG_RE.sub("-", str(path))


def config_root(config_dir: str | os.PathLike | None = None) -> Path:
    """Claude Code's config directory. Honours `CLAUDE_CONFIG_DIR`, which is
    how a sealed or throwaway run relocates its whole state - verified that a
    relocated config dir still receives `projects/<slug>/<id>.jsonl` with an
    unchanged event schema."""
    return Path(
        config_dir
        or os.environ.get("CLAUDE_CONFIG_DIR")
        or Path.home() / ".claude"
    )


def session_cwd(transcript: Path) -> str | None:
    """The `cwd` recorded inside a transcript, or None if it has no event
    carrying one. Reads only as far as the first hit; transcripts run to
    hundreds of megabytes in aggregate and are never read whole for this."""
    try:
        with transcript.open(errors="replace") as fh:
            for line in fh:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                cwd = event.get("cwd")
                if cwd:
                    return cwd
    except OSError:
        return None
    return None


def transcripts_for(
    repo: str | os.PathLike,
    *,
    config_dir: str | os.PathLike | None = None,
    exclude_session: str | None = None,
) -> list[Path]:
    """Transcripts belonging to `repo`, oldest first.

    `exclude_session` drops one session id - pass the live session's own id,
    whose transcript is still being written and is not yet a complete record
    of anything.
    """
    repo = str(Path(repo).resolve())
    directory = config_root(config_dir) / "projects" / slug(repo)
    if not directory.is_dir():
        return []

    found = []
    for path in directory.glob("*.jsonl"):
        if exclude_session and path.stem == exclude_session:
            continue
        # The slug is lossy, so a directory can in principle hold another
        # repo's sessions. Inheriting a different project's dead ends would
        # be worse than inheriting none, so confirm rather than assume.
        if session_cwd(path) != repo:
            continue
        found.append(path)
    return sorted(found, key=lambda p: p.stat().st_mtime)
