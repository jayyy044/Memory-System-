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


class AmbiguousMapping(Exception):
    """Raised when a mapping path finds more than one candidate commit for an
    issue, instead of silently picking one (e.g. the newest)."""

    def __init__(self, issue: int, shas: list[str]):
        self.issue = issue
        self.shas = shas
        super().__init__(f"issue #{issue}: {len(shas)} candidate commits, ambiguous")


def _one_or_raise(issue: int, shas: list[str]) -> str | None:
    shas = list(dict.fromkeys(shas))  # dedupe, keep order
    if not shas:
        return None
    if len(shas) > 1:
        raise AmbiguousMapping(issue, shas)
    return shas[0]


def map_issue_to_commit(repo_dir: Path, issue: int) -> str | None:
    """Three mapping paths, most specific first. Raises AmbiguousMapping if a
    path finds more than one candidate rather than guessing which is right."""
    # 1. merge commit from a fix-<issue> branch
    matches = []
    for line in _git(repo_dir, "log", "--all", "--merges", "--format=%H|%s").splitlines():
        sha, _, subject = line.partition("|")
        if re.search(rf"\bfix-{issue}\b", subject):
            matches.append(sha)
    result = _one_or_raise(issue, matches)
    if result:
        return result
    # 2. commit body closing the issue
    matches = []
    log = _git(repo_dir, "log", "--all", "--format=%H%x00%s %b%x01")
    for entry in log.split("\x01"):
        sha, _, msg = entry.partition("\x00")
        if re.search(rf"(fix|close[sd]?|resolve[sd]?)[^0-9]*#{issue}\b", msg, re.I):
            matches.append(sha.strip())
    result = _one_or_raise(issue, matches)
    if result:
        return result
    # 3. changelog pickaxe — the commit that changed whether the entry is
    # present. -S reports every commit where the occurrence count changes,
    # including a removal followed by a re-add (revert, reorg, regression
    # re-fix) — not just a single clean addition, so multiple hits are
    # ambiguous the same way paths 1/2 are, not a well-defined "oldest wins".
    shas = _git(repo_dir, "log", "--all", "--format=%H", "-S", f"issues/{issue})", "--", "CHANGES.md")
    matches = [s for s in shas.splitlines() if s.strip()]
    return _one_or_raise(issue, matches)


def changed_files(repo_dir: Path, sha: str, prefix: str = "liquid/") -> list[str]:
    out = _git(repo_dir, "diff", "--name-only", f"{sha}^1", sha)
    return sorted({f for f in out.splitlines() if f.startswith(prefix)})


def base_sha(repo_dir: Path, fix_sha: str) -> str:
    return _git(repo_dir, "rev-parse", f"{fix_sha}^1").strip()
