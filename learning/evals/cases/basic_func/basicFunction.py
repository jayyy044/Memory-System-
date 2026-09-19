from pathlib import Path


CASES = [
    {
        "name": "create_file",
        "setup": lambda d: None,
        "prompt": "create a file called notes.txt containing the word hello",
        "check": lambda d, out, history: (Path(d) / "notes.txt").exists()
                                 and "hello" in (Path(d) / "notes.txt").read_text(),
    },
]