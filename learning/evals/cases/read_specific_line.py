from pathlib import Path

SECRET = "hamburgers are so tasty"


def setup(d):
    lines = [f"line {i:04d}: {'x' * 60}" for i in range(600)]
    lines[449] = f"line 0450: SECRET={SECRET}"
    (Path(d) / "data.txt").write_text("\n".join(lines) + "\n")


def check(d, out, history):
    answered = SECRET in out
    used_read_file = any(
        c["function"]["name"] == "read_file"
        for m in history
        for c in (m.get("tool_calls") or [])
    )
    return answered and used_read_file


CASES = [
    {
        "name": "read_specific_line",
        "setup": setup,
        "prompt": "what is on line 450 of data.txt?",
        "check": check,
    },
]