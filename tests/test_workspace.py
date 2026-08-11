import subprocess
from pathlib import Path
from membench.workspace import provision, verify_sealed


def test_workspace_has_no_future_history(sample_task, tmp_path: Path):
    wd = provision(sample_task, tmp_path / "ws")
    log = subprocess.run(
        ["git", "log", "--all", "--format=%H"], cwd=wd, capture_output=True, text=True
    ).stdout.split()
    assert len(log) == 1, "shallow clone must expose exactly one commit"
    assert sample_task.fix_sha not in log


def test_workspace_strips_agent_instruction_files(sample_task, tmp_path: Path):
    wd = provision(sample_task, tmp_path / "ws")
    assert not (wd / "CLAUDE.md").exists()
    assert not (wd / "AGENTS.md").exists()


def test_verify_sealed_flags_planted_leak(sample_task, tmp_path: Path):
    wd = provision(sample_task, tmp_path / "ws")
    (wd / "CLAUDE.md").write_text("the fix is in loop.py")
    leaks = verify_sealed(wd)
    assert any("CLAUDE.md" in leak for leak in leaks)


def test_verify_sealed_clean_workspace(sample_task, tmp_path: Path):
    wd = provision(sample_task, tmp_path / "ws")
    assert verify_sealed(wd) == []
