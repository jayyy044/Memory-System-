"""Calibration arms. Every other arm asks "does memory help?"; these ask "is
this benchmark measuring anything at all?", so an arm here that quietly
degrades into FloorArm certifies a broken instrument instead of failing.

That is why both of the failure modes below RAISE rather than return a
degenerate preamble - same convention as `GateError` (membench/gate/checks.py:37)
and `SessionAError` (membench/session_a.py:26).
"""

from pathlib import Path

from membench.arms.floor import FloorArm
from membench.corpus.extract import BenchTask
from membench.models import Transcript


class CalibrationArmError(RuntimeError):
    """A calibration arm was asked to install memory it does not have. That is
    an orchestration/corpus bug, not a preamble: an oracle with no files to
    name, or a paths-only arm with no paths, is FloorArm wearing another arm's
    name, and the calibration check downstream would then compare an arm
    against itself and report a clean pass."""


class NullArm(FloorArm):
    """Floor under a second name, DELIBERATELY (D76). Null-vs-floor is the same
    condition run twice, so what it measures is the harness's own run-to-run
    variance - if the two disagree by more than sampling noise, the benchmark is
    not reproducible and no arm comparison from it means anything.

    It SUBCLASSES FloorArm rather than repeating `return ""`: with a copied body
    a later edit to FloorArm would make the two arms genuinely differ, and
    `check_null_equals_floor` would then go red for a reason that has nothing to
    do with reproducibility. Pinned by `NullArm.install is FloorArm.install`.
    """

    name = "null"


class OracleArm:
    """Hands over the golden answer's file list - the upper bound on what any
    memory could tell session B about WHERE the fix goes. If even this does not
    score high, the task is unsolvable by the agent and the whole corpus entry
    measures the model's ceiling, not memory.

    Note this calibrates a different axis from `PathsOnlyArm`: these paths come
    from `task.changed_files` (ground truth, which no real arm has), not from
    what session A happened to touch.
    """

    name = "oracle"

    def install(self, task: BenchTask, workdir: Path, notes_dir: Path) -> str:
        if not task.changed_files:
            raise CalibrationArmError(
                f"task {task.task_id!r} has an empty changed_files: an oracle arm with no files "
                f"to name is FloorArm plus a misleading sentence, and check_oracle_high would "
                f"then read as 'memory does not help'. Corpus bug, not an arm result."
            )
        files = "\n".join(f"- {f}" for f in task.changed_files)
        return (
            "A previous session determined the fix belongs in these files:\n"
            f"{files}\n"
            "Apply the fix there."
        )


class PathsOnlyArm:
    """Session A's touched-file list and NOTHING else - no notes on disk, no
    tool log, no prose (D77, for DEBT-7).

    The confound it isolates: `VaultArm` and `CeilingArm` both put session A's
    touched files into session B's prompt while `FloorArm` gives nothing, so
    `redone_files` is structurally higher for the memory arms because they were
    TOLD WHERE TO LOOK, not because memory caused repeated work. That could
    invert the benchmark's headline result and no unit test can catch it - every
    component behaves correctly.

    The comparison this enables (Task 9 owns it, not this module):
      paths_only - floor      = the effect of knowing where to look, alone
      vault      - paths_only = the effect of remembering the ATTEMPT, with the
                                file-list advantage held constant
    A vault-over-floor gain that is entirely reproduced by paths_only-over-floor
    means the benchmark is measuring file hints, not memory.

    Takes the `Transcript` at construction because that is what
    `generate_session_a` returns (D67) and the touched-file list is derived from
    `tool_calls`; parsing it back out of the written notes would couple this arm
    to session_a.py's markdown formatting.

    It takes the TASK ID at construction too, and checks it in `install`.
    `Transcript` carries no task id, so nothing in the transcript itself can say
    which task it came from - and one instance reused across tasks would hand
    every session B another task's file hints while looking perfectly healthy:
    a full preamble, real paths, no error, and the confound this arm exists to
    isolate measured against the wrong files.
    """

    name = "paths_only"

    def __init__(self, session_a: Transcript, task_id: str):
        self.session_a = session_a
        self.task_id = task_id

    def install(self, task: BenchTask, workdir: Path, notes_dir: Path) -> str:
        if task.task_id != self.task_id:
            raise CalibrationArmError(
                f"this arm carries session A for task {self.task_id!r} but was asked to install "
                f"it for task {task.task_id!r}. The file hints would be from the wrong task and "
                f"nothing downstream could tell - build one arm per task."
            )
        # dict.fromkeys, not set(): a file touched twice must be listed once,
        # and the ORDER session A touched them in is the only structure this arm
        # is allowed to carry.
        paths = list(dict.fromkeys(
            c.file_path for c in self.session_a.tool_calls if c.file_path
        ))
        if not paths:
            # Reachable: session_a.py's guards require tool CALLS, not file
            # paths - membench/session_a.py:178 renders "(none - it ran no
            # file-path tool)" for exactly this case.
            raise CalibrationArmError(
                f"task {task.task_id!r}: session A touched no file paths, so this arm has "
                f"nothing to install and is identical to FloorArm. The file-path confound it "
                f"exists to isolate would read as zero. Re-run session A, do not score this."
            )
        listed = "\n".join(f"- {p}" for p in paths)
        # Deliberately says nothing about WHAT was done to these files: tool
        # names, commands and prose all belong to the memory arms, and leaking
        # any of them here collapses the comparison above.
        return (
            "A previous session on this issue looked at these files:\n"
            f"{listed}\n"
            "Nothing else about that session is available."
        )
