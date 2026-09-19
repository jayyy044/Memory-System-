from pathlib import Path

SECRET = "alpine-quartz"


def setup(d):
    lines = [f"line {i:04d}: {'x' * 60}" for i in range(600)]
    lines[300] = f"line 0300: SECRET={SECRET}"
    (Path(d) / "data.txt").write_text("\n".join(lines) + "\n")


def check(d, out, history):
    norm = lambda s: "".join(c for c in s.lower() if c.isalnum())
    answered = norm(SECRET) in norm(out)
    capped = any(
        m.get("role") == "tool" and (
            "chars omitted" in (m.get("content") or "")
            or "call again with offset" in (m.get("content") or "")
        )
        for m in history
    )
    return answered and capped


CASES = [
    {
        "name": "truncated_output_recovery",
        "setup": setup,
        "prompt": "read data.txt and tell me what the SECRET value is",
        "check": check,
    },
]