"""load() answers tool calls that never got a result (session died mid-call)."""
import tempfile
from pathlib import Path

from transcripts import Transcript
from ._fixtures import SYS, USR, TEXT, call, result, write


def _load(msgs):
    d = tempfile.TemporaryDirectory()
    p = Path(d.name) / "s.jsonl"
    write(p, msgs)
    t = Transcript(p)
    t.load()
    return t, p, d


def one_unanswered():
    t, p, _d = _load([SYS, USR, call("a")])
    assert t[-1]["role"] == "tool", t[-1]
    assert t[-1]["tool_call_id"] == "a"
    assert "interrupted" in t[-1]["content"]
    assert len(p.read_text().splitlines()) == 4, "repair must be written to the file"


def three_calls_one_answered():
    t, p, _d = _load([SYS, USR, call("a", "b", "c"), result("a")])
    ids = [m["tool_call_id"] for m in t if m["role"] == "tool"]
    assert ids == ["a", "b", "c"], ids
    assert t[3]["content"] == "ok", "answered call must not be overwritten"


def clean_file_unchanged():
    t, p, _d = _load([SYS, USR, call("a"), result("a"), TEXT])
    assert len(t) == 5
    assert len(p.read_text().splitlines()) == 5


def no_tool_calls():
    t, p, _d = _load([SYS, USR, TEXT])
    assert len(t) == 3


def idempotent():
    t, p, _d = _load([SYS, USR, call("a")])
    t2 = Transcript(p)
    t2.load()
    assert len(t2) == 4, f"second load gave {len(t2)}, repair stacked"


CASES = [
    {"name": "repair_one_unanswered", "check": one_unanswered},
    {"name": "repair_three_calls_one_answered", "check": three_calls_one_answered},
    {"name": "repair_clean_unchanged", "check": clean_file_unchanged},
    {"name": "repair_no_tool_calls", "check": no_tool_calls},
    {"name": "repair_idempotent", "check": idempotent},
]
