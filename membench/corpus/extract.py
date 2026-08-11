import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class BenchTask:
    task_id: str
    repo: str
    issue_number: int
    issue_title: str
    issue_body: str
    base_sha: str
    fix_sha: str
    changed_files: list[str]
    fail_to_pass: list[str] = field(default_factory=list)
    pass_to_pass: list[str] = field(default_factory=list)


def _git(repo_dir: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo_dir, capture_output=True, text=True, check=False
    ).stdout


def map_issue_to_commit(repo_dir: Path, issue: int) -> str | None:
    """Three mapping paths, most specific first."""
    # 1. merge commit from a fix-<issue> branch
    for line in _git(repo_dir, "log", "--all", "--merges", "--format=%H|%s").splitlines():
        sha, _, subject = line.partition("|")
        if re.search(rf"\bfix-{issue}\b", subject):
            return sha
    # 2. commit body closing the issue
    log = _git(repo_dir, "log", "--all", "--format=%H%x00%s %b%x01")
    for entry in log.split("\x01"):
        sha, _, msg = entry.partition("\x00")
        if re.search(rf"(fix|close[sd]?|resolve[sd]?)[^0-9]*#{issue}\b", msg, re.I):
            return sha.strip()
    # 3. changelog pickaxe — the commit that added the entry
    shas = _git(repo_dir, "log", "--all", "--format=%H", "-S", f"issues/{issue})", "--", "CHANGES.md")
    lines = [s for s in shas.splitlines() if s.strip()]
    return lines[-1] if lines else None


def changed_files(repo_dir: Path, sha: str, prefix: str = "liquid/") -> list[str]:
    out = _git(repo_dir, "diff", "--name-only", f"{sha}^1", sha)
    return sorted({f for f in out.splitlines() if f.startswith(prefix)})


def base_sha(repo_dir: Path, fix_sha: str) -> str:
    return _git(repo_dir, "rev-parse", f"{fix_sha}^1").strip()
