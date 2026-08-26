"""Read Claude Code session transcripts for this project."""

from pathlib import Path


def projectFiles(transcriptPath) -> list[Path]:
    """Every OTHER session transcript for this project, oldest first.

    `transcriptPath` is the SessionStart payload's `transcript_path` - the
    live session's own transcript. Two things come free from it:

        folder       = .parent   (no slug encoding to reverse)
        live session = .stem     (the filename IS the session id)

    The live session is excluded. On `source: startup` its file does not
    exist yet so nothing is dropped; on resume/compact/clear/fork it does,
    and without this the session would read its own in-progress transcript
    back as if it were a finished one.

    Assumes every .jsonl in the folder belongs to this project. That holds
    unless two project paths encode to the same directory name (the
    encoding maps every non-[A-Za-z0-9-] character to '-', so '/x/my repo'
    and '/x/my-repo' would collide). Never observed - zero collisions
    across 60 project directories.
    """
    live = Path(transcriptPath)
    files = [p for p in live.parent.glob("*.jsonl") if p.stem != live.stem]
    return sorted(files, key=lambda p: p.stat().st_mtime)
