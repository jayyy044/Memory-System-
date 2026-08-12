from pathlib import Path
from typing import Protocol

from membench.corpus.extract import BenchTask

NOTES_MOUNT = ".membench-notes"

# The one retrieval instruction every on-disk-notes arm gives, defined once so
# GrepArm and VaultArm cannot drift apart on the thing they must both get right.
# `grep`, not `rg`: ripgrep is NOT in the agent image (docker/Dockerfile installs
# no such package) and an instruction naming a missing binary degrades the arm
# into FloorArm with a searchable directory the agent never manages to read. The
# explicit trailing path is load-bearing too - NOTES_MOUNT is a dotfile dir,
# which a bare recursive search of `.` can skip. Pinned by
# tests/test_arms.py::test_preamble_command_exists_and_works_in_the_agent_image,
# which runs this exact command line inside membench-agent:latest for BOTH arms.
SEARCH_HINT = f"Search them with `grep -rn <pattern> {NOTES_MOUNT}/` before starting."


class Arm(Protocol):
    name: str

    def install(self, task: BenchTask, workdir: Path, notes_dir: Path) -> str: ...
