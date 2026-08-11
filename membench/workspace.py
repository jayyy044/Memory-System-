import shutil
import subprocess
from pathlib import Path

from membench.corpus.extract import BenchTask

UPSTREAM = "https://github.com/jg-rp/liquid.git"

# Filenames searched for anywhere in the tree (I2: nested CLAUDE.md etc).
INSTRUCTION_NAMES = (
    "CLAUDE.md", "CLAUDE.local.md", "AGENTS.md", "GEMINI.md",
    ".cursorrules", ".clinerules", ".windsurfrules", "CONVENTIONS.md", ".mcp.json",
)
# Fixed paths (files or directories) checked only at repo root.
INSTRUCTION_PATHS = (
    ".claude", ".cursor/rules", ".github/copilot-instructions.md", ".github/instructions",
)


def _to_https(url: str) -> str:
    """.gitmodules commonly records SSH remotes (git@host:org/repo.git);
    provisioning must not depend on an SSH key being present."""
    if url.startswith("git@github.com:"):
        return "https://github.com/" + url[len("git@github.com:"):]
    return url


def _prune_to_single_commit(repo_dir: Path) -> None:
    """Fallback path only. Ref deletion alone leaves abandoned commits (e.g.
    the default branch tip fetched by the initial non-pinned clone) sitting
    in the object database (C3) — gc is required to actually purge them."""
    refs = subprocess.run(
        ["git", "for-each-ref", "--format=%(refname)"], cwd=repo_dir,
        capture_output=True, text=True, check=True,
    ).stdout.split()
    for ref in refs:
        subprocess.run(["git", "update-ref", "-d", ref], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "reflog", "expire", "--expire=now", "--all"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "gc", "--prune=now"], cwd=repo_dir, check=True, capture_output=True)


def _clone_fallback(url: str, sha: str, dest: Path) -> None:
    subprocess.run(["git", "clone", "--depth", "1", "--no-tags", url, str(dest)], check=True, capture_output=True)
    subprocess.run(["git", "fetch", "--depth", "1", "origin", sha], cwd=dest, check=True, capture_output=True)
    subprocess.run(["git", "checkout", "--detach", "FETCH_HEAD"], cwd=dest, check=True, capture_output=True)
    _prune_to_single_commit(dest)


def _clone_at(url: str, sha: str, dest: Path, *, force_fallback: bool = False) -> None:
    """Shallow-clone `url` at exactly `sha`, single commit visible.
    D16: `--branch` can never take a raw SHA, so that path is dead and
    dropped. Only two strategies remain: --revision (works on this git,
    2.55.0), or clone-default + targeted fetch + checkout, with every ref
    pruned and the object database gc'd afterward (D12/D16 corrected)."""
    if not force_fallback:
        r = subprocess.run(
            ["git", "clone", "--depth", "1", "--no-tags", "--revision", sha, url, str(dest)],
            capture_output=True, text=True,
        )
        if r.returncode == 0:
            return
    _clone_fallback(url, sha, dest)


def _submodule_entries(repo_dir: Path) -> list[tuple[str, str]]:
    """(relative path, url) pairs declared in repo_dir/.gitmodules, [] if none."""
    gm = repo_dir / ".gitmodules"
    if not gm.exists():
        return []
    paths = subprocess.run(
        ["git", "config", "-f", str(gm), "--get-regexp", r"submodule\..*\.path"],
        capture_output=True, text=True,
    ).stdout.splitlines()
    urls = dict(
        line.split(" ", 1)
        for line in subprocess.run(
            ["git", "config", "-f", str(gm), "--get-regexp", r"submodule\..*\.url"],
            capture_output=True, text=True,
        ).stdout.splitlines()
    )
    entries = []
    for line in paths:
        key, _, path = line.partition(" ")
        name = key[len("submodule."):-len(".path")]
        url = urls.get(f"submodule.{name}.url")
        if url:
            entries.append((path, url))
    return entries


def _provision_submodules(repo_dir: Path) -> None:
    """Checks out every submodule at the EXACT commit pinned by the
    superproject's tree (C1) — `git submodule update --depth 1` fetches the
    default branch tip instead, which for liquid is the post-fix oracle.
    Recurses for nested submodules."""
    for rel_path, raw_url in _submodule_entries(repo_dir):
        sub_dest = repo_dir / rel_path
        pinned = subprocess.run(
            ["git", "ls-tree", "HEAD", "--", rel_path], cwd=repo_dir,
            capture_output=True, text=True, check=True,
        ).stdout.split()
        if len(pinned) < 3 or pinned[1] != "commit":
            raise RuntimeError(f"submodule {rel_path} has no pinned commit in HEAD tree")
        pinned_sha = pinned[2]
        _clone_at(_to_https(raw_url), pinned_sha, sub_dest)
        subprocess.run(["git", "remote", "remove", "origin"], cwd=sub_dest, check=False, capture_output=True)
        _provision_submodules(sub_dest)


def _remove(p: Path) -> bool:
    if p.is_dir():
        shutil.rmtree(p)
        return True
    if p.exists() or p.is_symlink():
        p.unlink()
        return True
    return False


def _submodule_paths(repo_dir: Path) -> set[Path]:
    return {(repo_dir / p).resolve() for p, _ in _submodule_entries(repo_dir)}


def _strip_instruction_files(repo_dir: Path) -> None:
    """Removes agent instruction files/dirs and, if anything was tracked,
    folds the removal into the (single, shallow) HEAD commit — a plain
    unlink leaves the content in `git show HEAD:...` and a dirty tree (I1)."""
    sub_dirs = _submodule_paths(repo_dir)  # handled by their own repos, skip here
    changed = False
    for name in INSTRUCTION_NAMES:
        for hit in repo_dir.rglob(name):
            if ".git" in hit.parts:
                continue
            if any(hit.resolve() == sd or sd in hit.resolve().parents for sd in sub_dirs):
                continue
            if _remove(hit):
                changed = True
    for rel in INSTRUCTION_PATHS:
        if _remove(repo_dir / rel):
            changed = True
    if not changed:
        return
    subprocess.run(["git", "add", "-A"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.email=membench@local", "-c", "user.name=membench",
         "commit", "--amend", "--no-edit", "-q"],
        cwd=repo_dir, check=True, capture_output=True,
    )


def provision(task: BenchTask, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        shutil.rmtree(dest)
    _clone_at(UPSTREAM, task.base_sha, dest)
    subprocess.run(["git", "remote", "remove", "origin"], cwd=dest, check=False, capture_output=True)
    _provision_submodules(dest)
    _strip_instruction_files(dest)

    # D17: a workspace checked only by a separate gate ships unchecked
    # whenever that gate is skipped — provision() must self-verify and raise.
    leaks = verify_sealed(dest)
    if leaks:
        raise RuntimeError(f"provisioned workspace at {dest} failed verify_sealed: {leaks}")
    return dest


def _check_repo(repo_dir: Path, leaks: list[str], label: str) -> None:
    """Applies every seal check to one repo, then recurses into its
    submodules with the same checks (C1)."""
    if not (repo_dir / ".git").exists():
        leaks.append(f"{label}: no .git directory — cannot verify history, treating as unsealed")
        return

    log = subprocess.run(
        ["git", "log", "--all", "--format=%H"], cwd=repo_dir, capture_output=True, text=True,
    )
    head_sha = None
    if log.returncode != 0:
        leaks.append(f"{label}: git log failed (rc={log.returncode}): {log.stderr.strip()}")
    else:
        shas = log.stdout.split()
        if len(shas) != 1:
            leaks.append(f"{label}: history exposes {len(shas)} commits, expected 1")
        else:
            head_sha = shas[0]

    remotes = subprocess.run(["git", "remote"], cwd=repo_dir, capture_output=True, text=True)
    if remotes.returncode != 0:
        leaks.append(f"{label}: git remote failed (rc={remotes.returncode}): {remotes.stderr.strip()}")
    elif remotes.stdout.split():
        leaks.append(f"{label}: remotes present: {remotes.stdout.split()}")

    # C3: refs can be deleted while the objects they pointed at remain in
    # the object database (git doesn't auto-gc) — count commit objects
    # directly rather than trusting ref/log enumeration.
    objs = subprocess.run(
        ["git", "cat-file", "--batch-all-objects", "--batch-check=%(objectname) %(objecttype)"],
        cwd=repo_dir, capture_output=True, text=True,
    )
    if objs.returncode != 0:
        leaks.append(f"{label}: object scan failed (rc={objs.returncode}): {objs.stderr.strip()}")
    else:
        commits = [ln.split()[0] for ln in objs.stdout.splitlines() if ln.split()[1:2] == ["commit"]]
        if len(commits) != 1 or commits[0] != head_sha:
            leaks.append(
                f"{label}: object database contains commit objects {commits}, "
                f"expected exactly one matching HEAD ({head_sha})"
            )

    status = subprocess.run(["git", "status", "--porcelain"], cwd=repo_dir, capture_output=True, text=True)
    if status.returncode != 0:
        leaks.append(f"{label}: git status failed (rc={status.returncode}): {status.stderr.strip()}")
    elif status.stdout.strip():
        leaks.append(f"{label}: working tree not clean: {status.stdout.strip()!r}")

    sub_dirs = _submodule_paths(repo_dir)
    for name in INSTRUCTION_NAMES:
        for hit in repo_dir.rglob(name):
            if ".git" in hit.parts:
                continue
            if any(hit.resolve() == sd or sd in hit.resolve().parents for sd in sub_dirs):
                continue
            leaks.append(f"{label}: agent instruction file present: {hit.relative_to(repo_dir)}")
    for rel in INSTRUCTION_PATHS:
        if (repo_dir / rel).exists():
            leaks.append(f"{label}: agent instruction path present: {rel}")

    for rel_path, _ in _submodule_entries(repo_dir):
        _check_repo(repo_dir / rel_path, leaks, f"{label}/{rel_path}")


def verify_sealed(workdir: Path) -> list[str]:
    """Security check, not a lint (D13). Fails loud: any unexpected process
    state (nonzero exit from git, missing .git, an unscanned submodule) is
    itself reported as a leak, never silently swallowed into an empty list."""
    leaks: list[str] = []
    _check_repo(workdir, leaks, str(workdir))
    return leaks
