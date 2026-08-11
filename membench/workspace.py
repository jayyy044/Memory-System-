import subprocess
from pathlib import Path

from membench.corpus.extract import BenchTask

INSTRUCTION_FILES = ("CLAUDE.md", "AGENTS.md", ".cursorrules", ".github/copilot-instructions.md")
UPSTREAM = "https://github.com/jg-rp/liquid.git"


def _clone_at(sha: str, dest: Path) -> None:
    """Shallow-clone UPSTREAM at exactly `sha`, single commit visible.
    D12 fallback chain: --revision, then --branch, then depth-1 clone +
    targeted fetch + checkout. Whichever path works, the remote is removed
    afterward so nothing can later widen history."""
    r = subprocess.run(
        ["git", "clone", "--depth", "1", "--no-tags", "--revision", sha, UPSTREAM, str(dest)],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        r = subprocess.run(
            ["git", "clone", "--depth", "1", "--no-tags", "--branch", sha, UPSTREAM, str(dest)],
            capture_output=True, text=True,
        )
    if r.returncode != 0:
        subprocess.run(
            ["git", "clone", "--depth", "1", "--no-tags", UPSTREAM, str(dest)],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "fetch", "--depth", "1", "origin", sha], cwd=dest, check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "checkout", "FETCH_HEAD"], cwd=dest, check=True, capture_output=True,
        )


def provision(task: BenchTask, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    _clone_at(task.base_sha, dest)
    # Remove every remote so no later fetch can widen history.
    subprocess.run(["git", "remote", "remove", "origin"], cwd=dest, check=False, capture_output=True)
    subprocess.run(
        ["git", "submodule", "update", "--init", "--depth", "1"],
        cwd=dest, check=False, capture_output=True,
    )
    for rel in INSTRUCTION_FILES:
        p = dest / rel
        if p.exists():
            p.unlink()
    return dest


def verify_sealed(workdir: Path) -> list[str]:
    """Security check, not a lint (D13). Fails loud: any unexpected process
    state (nonzero exit from git, missing .git) is itself reported as a
    leak, never silently swallowed into an empty list."""
    leaks: list[str] = []

    if not (workdir / ".git").exists():
        leaks.append("no .git directory — cannot verify history, treating as unsealed")
        return leaks

    log = subprocess.run(
        ["git", "log", "--all", "--format=%H"], cwd=workdir, capture_output=True, text=True,
    )
    if log.returncode != 0:
        leaks.append(f"git log failed (rc={log.returncode}): {log.stderr.strip()}")
    else:
        shas = log.stdout.split()
        if len(shas) != 1:
            leaks.append(f"history exposes {len(shas)} commits, expected 1")

    remotes = subprocess.run(
        ["git", "remote"], cwd=workdir, capture_output=True, text=True,
    )
    if remotes.returncode != 0:
        leaks.append(f"git remote failed (rc={remotes.returncode}): {remotes.stderr.strip()}")
    elif remotes.stdout.split():
        leaks.append(f"remotes present: {remotes.stdout.split()}")

    for rel in INSTRUCTION_FILES:
        if (workdir / rel).exists():
            leaks.append(f"agent instruction file present: {rel}")

    return leaks
