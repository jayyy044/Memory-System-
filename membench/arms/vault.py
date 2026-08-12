import shutil
from pathlib import Path

from membench.arms.base import NOTES_MOUNT, SEARCH_HINT
from membench.corpus.extract import BenchTask


class VaultArm:
    """The filesystem-notes vault: `current.md` is pushed into the prompt as
    standing state, the rest stays on disk to be searched. Grep arm plus a
    curated header - that header is the only thing distinguishing the two, so
    it must survive an absent `current.md` without collapsing into GrepArm's
    exact behaviour."""

    name = "vault"

    def install(self, task: BenchTask, workdir: Path, notes_dir: Path) -> str:
        dest = workdir / NOTES_MOUNT
        # symlinks=False: see GrepArm. Pinned separately by
        # tests/test_arms.py::test_vault_copied_notes_never_carry_a_symlink_into_the_workspace -
        # a comment pointing at another module's test does not fail when THIS
        # call is the one someone "improves" to symlinks=True.
        shutil.copytree(notes_dir, dest, dirs_exist_ok=True)
        current = dest / "current.md"
        header = current.read_text() if current.is_file() else "(no current.md was written)"
        # SEARCH_HINT, not just the directory path: pointing at a dotfile dir
        # the agent may never think to search is the same defect as naming a
        # binary the image does not have - the notes are on disk and never read,
        # and the arm degrades toward FloorArm. Shared with GrepArm so the two
        # cannot drift; what still distinguishes this arm is the front-loaded
        # current.md header above, which is the whole point of the vault.
        return (
            "Project state from previous sessions:\n\n"
            f"{header}\n\n"
            f"Further notes are in ./{NOTES_MOUNT}/. {SEARCH_HINT}"
        )
