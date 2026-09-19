import re
from pathlib import Path


def setup(d):
    src = Path(d) / "src"
    src.mkdir()
    (src / "main.py").write_text("print(1)\n")


CASES = [
    {
        "name": "nested_file_no_cd",
        "setup": setup,
        "prompt": "how many lines are in main.py?",
        "check": lambda d, out, history: re.search(r"\b1\b", out) is not None,
    },
]