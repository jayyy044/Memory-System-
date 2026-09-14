from pathlib import Path


def setup(d):
    for i in range(12):
        (Path(d) / f"file{i:02d}.txt").write_text(f"{i}\n" * (i + 1))


def check(d, out, history):
    text_answer = isinstance(out, str) and out.strip() != ""
    ended_clean = not history[-1].get("tool_calls")
    used_tools = any(m.get("role") == "tool" for m in history)
    return text_answer and ended_clean and used_tools


CASES = [
    {
        "name": "budget_exhaustion_ends_in_text",
        "setup": setup,
        "max_hops": 2,
        "prompt": (
            "Count the lines in each .txt file in this directory. "
            "Use a separate bash call for each file, one file per call. "
            "Do not combine them."
        ),
        "check": check,
    },
]