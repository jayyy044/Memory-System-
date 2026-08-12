from pathlib import Path

from membench.corpus.extract import BenchTask


class CeilingArm:
    """Every note inlined into the prompt - perfect recall, no retrieval step.
    The upper bound the retrieval arms are compared against, so it deliberately
    plants nothing in the workspace."""

    name = "ceiling"

    def install(self, task: BenchTask, workdir: Path, notes_dir: Path) -> str:
        chunks = [
            f"--- {p.relative_to(notes_dir).as_posix()} ---\n{p.read_text()}"
            for p in sorted(notes_dir.rglob("*.md"))
        ]
        return "Notes from a previous session on this task:\n\n" + "\n\n".join(chunks)
