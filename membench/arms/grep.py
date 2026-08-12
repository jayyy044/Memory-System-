import shutil
from pathlib import Path

from membench.arms.base import NOTES_MOUNT, SEARCH_HINT
from membench.corpus.extract import BenchTask


class GrepArm:
    """Session A's notes are on disk and searchable, but nothing is in the
    prompt: the agent has to go look."""

    name = "grep"

    def install(self, task: BenchTask, workdir: Path, notes_dir: Path) -> str:
        # symlinks=False (the default, pinned by
        # tests/test_arms.py::test_copied_notes_never_carry_a_symlink_into_the_workspace):
        # copytree DEREFERENCES, so a link inside notes_dir lands as a plain
        # file with real content. It must stay that way - the notes exist to
        # be READ by session B, and a copied-through link is content the arm
        # only maybe delivers: dangling if its target was outside notes_dir,
        # or pointing at host paths that do not exist inside the container.
        # (It is NOT voided by runner._contained_path - that is only reached
        # for gold-restore paths and test-surface entries, and .membench-notes
        # is neither, verified by running _reset_test_surface over a notes dir
        # containing symlinks: no RunTestsError, links untouched.)
        shutil.copytree(notes_dir, workdir / NOTES_MOUNT, dirs_exist_ok=True)
        return (
            f"Notes from a previous session on this task are in ./{NOTES_MOUNT}/. "
            f"{SEARCH_HINT}"
        )
