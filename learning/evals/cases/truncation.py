from pathlib import Path

SECRET = "alpine-quartz"


def setup(d):
    lines = [f"line {i:04d}: {'x' * 60}" for i in range(600)]
    lines[587] = f"line 0587: SECRET={SECRET}"
    (Path(d) / "data.txt").write_text("\n".join(lines) + "\n")


def check(d, out, history):
    answered = SECRET in out
    truncated = any(
        m.get("role") == "tool" and "[truncated" in (m.get("content") or "")
        for m in history
    )
    return answered and truncated


CASES = [
    {
        "name": "truncated_output_recovery",
        "setup": setup,
        "prompt": "read data.txt and tell me what the SECRET value is",
        "check": check,
    },
]