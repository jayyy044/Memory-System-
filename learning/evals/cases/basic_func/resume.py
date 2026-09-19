from pathlib import Path
from transcripts import Transcript
import json

SECRET = "alpine-quartz"
SYSTEM = (
    "You are a coding agent working inside a git repository. Your job is to "
    "complete the user's task, not to explore the repository.\n\n"
    "Rules:\n"
    "- If the task is genuinely ambiguous, ask one short question. Otherwise act."
)

def setup(d):
    # data.txt exists so re-reading is possible — and shows up as a wasted hop
    lines = [f"line {i:04d}: {'x' * 60}" for i in range(600)]
    lines[449] = f"line 0450: SECRET={SECRET}"
    (Path(d) / "data.txt").write_text("\n".join(lines) + "\n")

    # session 1, by hand: the secret was already found and answered
    session = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": "what is on line 450 of data.txt?"},
        {"role": "assistant", "content": None, "tool_calls": [{
            "id": "fc_1", "type": "function",
            "function": {"name": "read_file",
                         "arguments": '{"path": "data.txt", "offset": 450, "limit": 1}'}}]},
        {"role": "tool", "tool_call_id": "fc_1",
         "content": f"   450  line 0450: SECRET={SECRET}\n\n[showing 450-450 of 600 lines]"},
        {"role": "assistant", "content": f"Line 450 of data.txt is: SECRET={SECRET}"},
    ]
    (Path(d) / "session.jsonl").write_text(
        "\n".join(json.dumps(m) for m in session) + "\n")


def history(d):
    t = Transcript(Path(d) / "session.jsonl")
    t.load()
    return t


def check(d, out, history):
    new_hops = sum(1 for m in history if m["role"] == "tool") - 1   # session 1 had one
    norm = lambda s: "".join(c for c in s.lower() if c.isalnum())   # model turns '-' into U+2011
    return norm(SECRET) in norm(out) and new_hops == 0


CASES = [{
    "name": "resume_uses_context",
    "setup": setup,
    "history": history,          # new key — runner loads this instead of a fresh list
    "prompt": "what was the secret again?",
    "check": check,
}]