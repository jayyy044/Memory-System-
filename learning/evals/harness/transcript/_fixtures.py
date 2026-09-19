"""Message shapes shared by the transcript cases."""
import json

SYS = {"role": "system", "content": "s"}
USR = {"role": "user", "content": "u"}
TEXT = {"role": "assistant", "content": "done"}


def call(*ids):
    return {"role": "assistant", "content": None, "tool_calls": [
        {"id": i, "type": "function", "function": {"name": "bash", "arguments": "{}"}}
        for i in ids]}


def result(i):
    return {"role": "tool", "tool_call_id": i, "content": "ok"}


def write(path, msgs):
    path.write_text("\n".join(json.dumps(m) for m in msgs) + "\n")
