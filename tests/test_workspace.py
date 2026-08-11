import subprocess
from pathlib import Path
from membench.corpus.extract import BenchTask
from membench.workspace import (
    provision,
    verify_sealed,
    UPSTREAM,
    _clone_at,
    _provision_submodules,
    _seal_repo,
)

# golden-liquid tip at the fix commit — the post-fix oracle a leaking
# submodule would expose (D15). Pinned commit at sample_task.base_sha is
# b6386e7adf964517546fec6564ef36e12c4b498e (verified against the real repo).
GOLDEN_LIQUID_POST_FIX_TIP = "65c2f76ea64ef20647c295b000df5fcd9fc471cd"
GOLDEN_LIQUID_PINNED = "b6386e7adf964517546fec6564ef36e12c4b498e"


def _init_local_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t.com"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "t"], check=True)


def _seed_commit(path: Path) -> str:
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-q", "-m", "seed"], check=True)
    return subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()


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


# --- C1: submodule pinned to base_sha, post-fix oracle unreachable ---------

def test_submodule_pinned_to_base_sha_not_default_tip(sample_task, tmp_path: Path):
    wd = provision(sample_task, tmp_path / "ws")
    sub = wd / "tests" / "golden-liquid"
    pinned_in_tree = subprocess.run(
        ["git", "ls-tree", "HEAD", "--", "tests/golden-liquid"], cwd=wd,
        capture_output=True, text=True, check=True,
    ).stdout.split()[2]
    assert pinned_in_tree == GOLDEN_LIQUID_PINNED
    sub_head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=sub, capture_output=True, text=True, check=True
    ).stdout.strip()
    assert sub_head == pinned_in_tree, "submodule must be at the pinned SHA, not the default branch tip"

    # oracle object from the post-fix commit must not be reachable/present
    post_fix = subprocess.run(["git", "cat-file", "-e", GOLDEN_LIQUID_POST_FIX_TIP], cwd=sub, capture_output=True)
    assert post_fix.returncode != 0, "post-fix golden-liquid oracle must not be present in the submodule"

    assert verify_sealed(wd) == []


# --- C2/D16: dead --branch fallback dropped; fetch+checkout path is sealed -

def test_fallback_clone_path_produces_sealed_workspace(sample_task, tmp_path: Path):
    dest = tmp_path / "fb"
    _clone_at(UPSTREAM, sample_task.base_sha, dest, force_fallback=True)
    subprocess.run(["git", "remote", "remove", "origin"], cwd=dest, check=False, capture_output=True)
    _provision_submodules(dest)

    log = subprocess.run(
        ["git", "log", "--all", "--format=%H"], cwd=dest, capture_output=True, text=True
    ).stdout.split()
    assert log == [sample_task.base_sha]
    assert sample_task.fix_sha not in log
    assert verify_sealed(dest) == []


# --- C3: object database checked directly, not just refs -------------------

def test_verify_sealed_flags_dangling_commit_objects(sample_task, tmp_path: Path):
    """Simulates what the pre-fix fallback path left behind: refs deleted
    but the abandoned commit's objects still present in the object DB."""
    dest = tmp_path / "dangling"
    subprocess.run(["git", "clone", "--depth", "1", "--no-tags", UPSTREAM, str(dest)], check=True, capture_output=True)
    subprocess.run(["git", "fetch", "--depth", "1", "origin", sample_task.base_sha], cwd=dest, check=True, capture_output=True)
    subprocess.run(["git", "checkout", "--detach", "FETCH_HEAD"], cwd=dest, check=True, capture_output=True)
    # unlike _prune_to_single_commit, only delete the ref — leave the object DB dirty
    refs = subprocess.run(
        ["git", "for-each-ref", "--format=%(refname)"], cwd=dest, capture_output=True, text=True, check=True
    ).stdout.split()
    for ref in refs:
        subprocess.run(["git", "update-ref", "-d", ref], cwd=dest, check=True, capture_output=True)
    subprocess.run(["git", "remote", "remove", "origin"], cwd=dest, check=False, capture_output=True)

    log = subprocess.run(["git", "log", "--all", "--format=%H"], cwd=dest, capture_output=True, text=True).stdout.split()
    assert log == [sample_task.base_sha], "ref-level view looks sealed"

    leaks = verify_sealed(dest)
    assert leaks, "object database still holds the abandoned commit; verify_sealed must catch it"
    assert any("object database" in leak for leak in leaks)


# --- I1: instruction files removed from HEAD, not just unlinked ------------

def test_strips_instruction_file_from_history_and_leaves_clean_tree(tmp_path: Path):
    src = tmp_path / "src"
    _init_local_repo(src)
    (src / "CLAUDE.md").write_text("the fix is in loop.py")
    (src / "real.py").write_text("x = 1\n")
    sha = _seed_commit(src)

    dest = tmp_path / "ws"
    _clone_at(str(src), sha, dest)
    _seal_repo(dest)

    assert not (dest / "CLAUDE.md").exists()
    show = subprocess.run(["git", "show", "HEAD:CLAUDE.md"], cwd=dest, capture_output=True, text=True)
    assert show.returncode != 0, "instruction file must be gone from HEAD, not just the working tree"
    status = subprocess.run(["git", "status", "--porcelain"], cwd=dest, capture_output=True, text=True).stdout
    assert status.strip() == "", "removal must not leave the tree dirty"


# --- I2: broader filename list, nested paths, directories ------------------

def test_strips_nested_and_directory_instruction_paths(tmp_path: Path):
    src = tmp_path / "src2"
    _init_local_repo(src)
    (src / "sub").mkdir()
    (src / "sub" / "CLAUDE.md").write_text("nested")
    (src / ".claude").mkdir()
    (src / ".claude" / "settings.json").write_text("{}")
    (src / ".cursor" / "rules").mkdir(parents=True)
    (src / ".cursor" / "rules" / "x.mdc").write_text("rule")
    (src / "GEMINI.md").write_text("g")
    (src / "real.py").write_text("x = 1\n")
    sha = _seed_commit(src)

    dest = tmp_path / "ws2"
    _clone_at(str(src), sha, dest)
    _seal_repo(dest)

    assert not (dest / "sub" / "CLAUDE.md").exists()
    assert not (dest / ".claude").exists()
    assert not (dest / ".cursor" / "rules").exists()
    assert not (dest / "GEMINI.md").exists()
    assert (dest / "real.py").exists()
    status = subprocess.run(["git", "status", "--porcelain"], cwd=dest, capture_output=True, text=True).stdout
    assert status.strip() == ""


# --- I3: fail-loud branches exercised ---------------------------------------

def test_verify_sealed_missing_git_dir_is_a_leak(tmp_path: Path):
    empty = tmp_path / "not_a_repo"
    empty.mkdir()
    leaks = verify_sealed(empty)
    assert leaks, "unknown state must never be reported as sealed"
    assert any("no .git" in leak for leak in leaks)


def test_verify_sealed_git_failure_is_a_leak(tmp_path: Path):
    broken = tmp_path / "broken_repo"
    broken.mkdir()
    (broken / ".git").write_text("not a real git dir")
    leaks = verify_sealed(broken)
    assert leaks, "a broken repo must never be reported as sealed ([] is a pass)"


# --- I1 (fix round 2): amend alone leaves the pre-amend blob reachable by
# SHA — must purge (reflog expire + gc) after rewriting history. Goes
# through the real provision() -> verify_sealed() path end to end, not just
# the stripping helper in isolation (that's exactly what let the bug through
# fix round 1's tests).

def test_provision_purges_amended_instruction_file_end_to_end(tmp_path: Path):
    src = tmp_path / "src3"
    _init_local_repo(src)
    (src / "CLAUDE.md").write_text("SECRET: the fix is in loop.py, change line 42")
    (src / "real.py").write_text("x = 1\n")
    pre_amend_sha = _seed_commit(src)

    task = BenchTask(
        task_id="synthetic-i1", repo="local/synthetic", issue_number=1,
        issue_title="", issue_body="", base_sha=pre_amend_sha, fix_sha="0" * 40, changed_files=[],
    )
    wd = provision(task, tmp_path / "ws3", url=str(src))

    show = subprocess.run(
        ["git", "show", f"{pre_amend_sha}:CLAUDE.md"], cwd=wd, capture_output=True, text=True
    )
    assert show.returncode != 0, "pre-amend commit's blob must be unreachable by SHA after purge"
    assert verify_sealed(wd) == [], "provision()'s own seal check must pass, not just log/status"


# --- F1: stripping and checking must agree about submodules ----------------

def test_seals_instruction_file_inside_submodule(tmp_path: Path):
    sub_src = tmp_path / "subsrc"
    _init_local_repo(sub_src)
    (sub_src / "CLAUDE.md").write_text("leaked from submodule")
    (sub_src / "data.txt").write_text("d")
    _seed_commit(sub_src)

    parent_src = tmp_path / "parentsrc"
    _init_local_repo(parent_src)
    subprocess.run(
        ["git", "-C", str(parent_src), "-c", "protocol.file.allow=always",
         "submodule", "add", str(sub_src), "vendor/sub"],
        check=True, capture_output=True,
    )
    parent_sha = _seed_commit(parent_src)

    dest = tmp_path / "ws4"
    _clone_at(str(parent_src), parent_sha, dest)
    subprocess.run(["git", "remote", "remove", "origin"], cwd=dest, check=False, capture_output=True)
    _provision_submodules(dest)
    _seal_repo(dest)

    assert not (dest / "vendor" / "sub" / "CLAUDE.md").exists()
    assert verify_sealed(dest) == [], "checking must not raise on a leak that stripping already fixed"

    sub_head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=dest / "vendor" / "sub", capture_output=True, text=True, check=True
    ).stdout.strip()
    pinned = subprocess.run(
        ["git", "ls-tree", "HEAD", "--", "vendor/sub"], cwd=dest, capture_output=True, text=True, check=True
    ).stdout.split()[2]
    assert pinned == sub_head, "parent gitlink must follow the submodule's amended HEAD, not drift from it"


# --- F2: directory-shaped instruction carriers below repo root -------------

def test_strips_instruction_dir_nested_below_root(tmp_path: Path):
    src = tmp_path / "src6"
    _init_local_repo(src)
    (src / "sub" / ".claude").mkdir(parents=True)
    (src / "sub" / ".claude" / "settings.json").write_text("{}")
    (src / "real.py").write_text("x = 1\n")
    sha = _seed_commit(src)

    dest = tmp_path / "ws6"
    _clone_at(str(src), sha, dest)
    subprocess.run(["git", "remote", "remove", "origin"], cwd=dest, check=False, capture_output=True)
    _seal_repo(dest)

    assert not (dest / "sub" / ".claude").exists()
    assert (dest / "real.py").exists()
    assert verify_sealed(dest) == []


# --- F3: symlink to a directory must not crash rmtree -----------------------

def test_removes_symlink_to_directory_without_crashing(tmp_path: Path):
    src = tmp_path / "src7"
    _init_local_repo(src)
    (src / "real_target").mkdir()
    (src / "real_target" / "f.txt").write_text("x")
    (src / ".claude").symlink_to("real_target", target_is_directory=True)
    (src / "real.py").write_text("x = 1\n")
    sha = _seed_commit(src)

    dest = tmp_path / "ws7"
    _clone_at(str(src), sha, dest)
    _seal_repo(dest)  # must not raise OSError

    assert not (dest / ".claude").exists()
    assert (dest / "real.py").exists()
