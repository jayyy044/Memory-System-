import json
import tempfile
from pathlib import Path

from transcripts import Transcript
from ._fixtures import SYS, USR, TEXT, call, result


def round_trip():
    """What was appended is exactly what loads back."""
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "s.jsonl"
        t = Transcript(p)
        msgs = [SYS, USR, call("a"), result("a"), TEXT]
        for m in msgs:
            t.append(m)
        t2 = Transcript(p)
        t2.load()
        assert list(t2) == msgs, f"loaded {len(t2)} msgs, expected {len(msgs)}"


def append_after_load():
    """Appending to a loaded transcript grows the file by one line, earlier lines untouched."""
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "s.jsonl"
        t = Transcript(p)
        for m in (SYS, USR, TEXT):
            t.append(m)
        t2 = Transcript(p)
        t2.load()
        before = p.read_text()
        t2.append(USR)
        assert p.read_text() == before + json.dumps(USR, ensure_ascii=False) + "\n"


CASES = [
    {"name": "transcript_round_trip", "check": round_trip},
    {"name": "transcript_append_after_load", "check": append_after_load},
]
