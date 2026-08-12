from pathlib import Path

from membench.corpus.extract import BenchTask


class FloorArm:
    """No memory at all: session B starts from the issue text alone. The
    control every other arm is measured against."""

    name = "floor"

    def install(self, task: BenchTask, workdir: Path, notes_dir: Path) -> str:
        return ""
