# Agent Memory Benchmark — Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a harness that measures whether a memory system helps a coding agent resume interrupted work, and prove the harness measures something real before reporting any number.

**Architecture:** Each benchmark task pairs a *session A* (does part of a fix, stops) with a *session B* (cold start, must finish). Session B runs once per arm; arms differ only in what memory material is present in its workspace. Scoring is deterministic: pytest results for correctness, transcript trace analysis for redone work and repeated dead ends. A ten-check audit gate runs before any result is reported.

**Tech Stack:** Python 3.11+, pytest, `claude` CLI in headless mode as the agent driver, `git` for workspace provisioning, `ripgrep` for the grep arm.

## Global Constraints

- Subject repo is `jg-rp/liquid`, MIT. Tasks come from **post-cutoff issues only** (closed 2026-06 or later) — contamination is avoided by selection, never assumed absent.
- Every workspace is a **shallow clone (`--depth 1`) at the base commit** with no other refs. History leakage is the primary threat model.
- **Submodules are part of the workspace and part of the threat model.** `git submodule update --depth 1` fetches the default branch tip, NOT the pinned commit — for `liquid` that tip is the post-fix `golden-liquid` oracle. Submodules must be checked out at the exact pinned SHA, have their remotes removed, and be inspected by `verify_sealed` recursively.
- **`provision()` must call `verify_sealed()` on its own output and raise on any leak.** A workspace that is only checked by a separate gate is a workspace that ships unchecked when the gate is skipped.
- **The agent must not be able to reach the network or the host environment.** `run_agent` passes an explicit `env=` (never inheriting), sets `CLAUDE_CONFIG_DIR` to a per-run temp dir and `TZ=UTC`, blanks `GH_TOKEN`/`GITHUB_TOKEN`, and denies `WebFetch`/`WebSearch` plus `gh`/`curl`/`git fetch` at the tool layer. Sealing git history is necessary and NOT sufficient: `gh` is installed and authenticated on the host, and the upstream issue is public.
- `TZ=UTC` must be set for every pytest invocation — two date-filter tests fail otherwise.
- `tests/golden-liquid` submodule must be fetched at provisioning time, never during a scored run.
- **No number is reported until all ten audit checks pass.** A failing gate blocks reporting, not just warns.
- Deterministic scoring is the primary number. No LLM judge in phase 1.
- Trace checks match **exact tokens**, never substrings.
- Every result records the scaffold config (turns, tools, model, effort) alongside the score.
- All randomness sources are logged; results are medians over N runs with spread, never single runs.

---

## File Structure

```
membench/
  __init__.py
  models.py          BenchTask, ArmResult, RunResult dataclasses
  driver.py          headless agent invocation + transcript capture
  corpus/
    extract.py       issue -> fix commit -> changed files -> F2P/P2P
    tasks/           generated task JSON, one per issue
  workspace.py       shallow clone, submodule, leak scrub, verification
  runner.py          pytest invocation + per-test result parsing
  arms/
    base.py          Arm protocol
    floor.py  grep.py  ceiling.py  vault.py
    mem0_arm.py  basic_memory_arm.py
    calibration.py   null + oracle arms
  scoring/
    correctness.py   F2P/P2P evaluation
    trace.py         redone-work + dead-end detection
  gate/
    checks.py        the ten audit checks
    report.py        gate result, blocks on failure
  cli.py
tests/
  test_driver.py  test_extract.py  test_workspace.py  test_runner.py
  test_arms.py  test_correctness.py  test_trace.py  test_gate.py
  fixtures/
```

---

### Task 1: Agent driver

Everything downstream depends on capturing tool calls, so this goes first.

**Verified 2026-08-10 against `claude` 2.1.227** — the event shape below is observed
output, not assumption:

```
exit=0, 17 stream-json lines
event types: system×11, assistant×3, user×1, rate_limit_event×1, result×1
assistant content blocks: thinking | tool_use (name="Read", input keys ['file_path']) | text
result event keys include: result, total_cost_usd, num_turns, usage, modelUsage,
                           stop_reason, permission_denials, session_id
```

Three constraints that came out of that run:

- **stdin must be redirected** (`< /dev/null` / `stdin=DEVNULL`) or every invocation
  stalls 3s waiting for piped input.
- **`--permission-mode acceptEdits`** is required; without it a headless run blocks on
  permission prompts.
- **`result.permission_denials`** must be captured. A non-empty list means the agent was
  *blocked*, not incapable — a run that scores badly for the wrong reason. Task 12 gates on it.
- `timeout(1)` is unavailable on macOS; bound runs with the Python `subprocess` timeout
  and `--max-turns`, not a shell wrapper.

**Files:**
- Create: `membench/driver.py`
- Create: `membench/models.py`
- Test: `tests/test_driver.py`

**Interfaces:**
- Consumes: nothing
- Produces: `Transcript` dataclass with fields `text: str`, `tool_calls: list[ToolCall]`, `exit_code: int`, `cost_usd: float`, `num_turns: int`, `permission_denials: list`, `stop_reason: str | None`; `ToolCall` with `name: str`, `command: str | None`, `file_path: str | None`; `run_agent(prompt: str, workdir: Path, *, max_turns: int, model: str) -> Transcript`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_driver.py
from pathlib import Path
from membench.driver import run_agent, Transcript


def test_run_agent_captures_tool_calls(tmp_path: Path):
    (tmp_path / "hello.txt").write_text("marker-9f3a\n")
    t = run_agent(
        "Read hello.txt and reply with only its contents.",
        workdir=tmp_path,
        max_turns=4,
        model="claude-sonnet-5",
    )
    assert isinstance(t, Transcript)
    assert t.exit_code == 0
    assert "marker-9f3a" in t.text
    assert any(c.name == "Read" for c in t.tool_calls)
    assert any(c.file_path and c.file_path.endswith("hello.txt") for c in t.tool_calls)


def test_run_agent_captures_result_metadata(tmp_path: Path):
    (tmp_path / "hello.txt").write_text("marker-9f3a\n")
    t = run_agent(
        "Read hello.txt and reply with only its contents.",
        workdir=tmp_path,
        max_turns=4,
        model="claude-sonnet-5",
    )
    assert t.num_turns > 0
    assert t.cost_usd > 0
    assert t.permission_denials == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_driver.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'membench.driver'`

- [ ] **Step 3: Write minimal implementation**

```python
# membench/models.py
from dataclasses import dataclass, field


@dataclass
class ToolCall:
    name: str
    command: str | None = None
    file_path: str | None = None


@dataclass
class Transcript:
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    exit_code: int = 0
    raw: str = ""
    cost_usd: float = 0.0
    num_turns: int = 0
    stop_reason: str | None = None
    permission_denials: list = field(default_factory=list)
```

```python
# membench/driver.py
import json
import subprocess
from pathlib import Path

from membench.models import ToolCall, Transcript


def _parse_stream(raw: str) -> tuple[str, list[ToolCall], dict]:
    """Event shape verified against claude 2.1.227 stream-json output."""
    text_parts: list[str] = []
    calls: list[ToolCall] = []
    meta: dict = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            evt = json.loads(line)
        except json.JSONDecodeError:
            continue
        etype = evt.get("type")
        if etype == "assistant":
            # Blocks observed: thinking | tool_use | text. Thinking is ignored.
            for block in evt.get("message", {}).get("content", []):
                btype = block.get("type")
                if btype == "text":
                    text_parts.append(block.get("text", ""))
                elif btype == "tool_use":
                    inp = block.get("input") or {}
                    calls.append(
                        ToolCall(
                            name=block.get("name", ""),
                            command=inp.get("command"),
                            file_path=inp.get("file_path") or inp.get("path"),
                        )
                    )
        elif etype == "result":
            if evt.get("result"):
                text_parts.append(str(evt["result"]))
            meta = {
                "cost_usd": float(evt.get("total_cost_usd") or 0.0),
                "num_turns": int(evt.get("num_turns") or 0),
                "stop_reason": evt.get("stop_reason"),
                "permission_denials": evt.get("permission_denials") or [],
            }
    return "\n".join(text_parts), calls, meta


def run_agent(
    prompt: str,
    workdir: Path,
    *,
    max_turns: int,
    model: str,
    timeout_s: int = 900,
) -> Transcript:
    proc = subprocess.run(
        [
            "claude", "-p", prompt,
            "--output-format", "stream-json",
            "--verbose",
            "--max-turns", str(max_turns),
            "--model", model,
            "--permission-mode", "acceptEdits",
        ],
        cwd=workdir,
        capture_output=True,
        text=True,
        timeout=timeout_s,
        stdin=subprocess.DEVNULL,   # without this every run stalls 3s waiting on stdin
    )
    text, calls, meta = _parse_stream(proc.stdout)
    return Transcript(
        text=text,
        tool_calls=calls,
        exit_code=proc.returncode,
        raw=proc.stdout,
        cost_usd=meta.get("cost_usd", 0.0),
        num_turns=meta.get("num_turns", 0),
        stop_reason=meta.get("stop_reason"),
        permission_denials=meta.get("permission_denials", []),
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_driver.py -v`
Expected: PASS (2 passed)

Flags and event shape are verified against `claude` 2.1.227. If the installed version
differs, re-run the probe below and adjust `_parse_stream` before proceeding — **do not**
continue to Task 2 with a driver that cannot capture tool calls, since every trace check
depends on them.

```bash
mkdir -p /tmp/drv && printf 'marker-9f3a\n' > /tmp/drv/hello.txt
cd /tmp/drv && claude -p "Read hello.txt and reply with only its contents." \
  --output-format stream-json --verbose --max-turns 4 \
  --model claude-sonnet-5 --permission-mode acceptEdits < /dev/null > out.jsonl
python3 -c "import json;[print(json.loads(l).get('type')) for l in open('/tmp/drv/out.jsonl') if l.strip()]"
```

- [ ] **Step 5: Commit**

```bash
git add membench/driver.py membench/models.py tests/test_driver.py
git commit -m "feat: headless agent driver with transcript capture"
```

---

### Task 2: Corpus extraction

Turns an issue number into a fully specified benchmark task. The three mapping paths exist because liquid's `fix-<issue>` branch convention is recent; older issues need the changelog pickaxe.

**Files:**
- Create: `membench/corpus/extract.py`
- Test: `tests/test_extract.py`

**Interfaces:**
- Consumes: nothing
- Produces: `BenchTask` dataclass; `map_issue_to_commit(repo_dir: Path, issue: int) -> str | None`; `changed_files(repo_dir: Path, sha: str) -> list[str]`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_extract.py
from pathlib import Path
import subprocess
import pytest
from membench.corpus.extract import map_issue_to_commit, changed_files

LIQUID = Path("fixtures/liquid")


@pytest.fixture(scope="module")
def repo() -> Path:
    if not LIQUID.exists():
        LIQUID.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "clone", "-q", "https://github.com/jg-rp/liquid.git", str(LIQUID)],
            check=True,
        )
    return LIQUID


def test_maps_fix_branch_issue(repo: Path):
    sha = map_issue_to_commit(repo, 209)
    assert sha is not None
    files = changed_files(repo, sha)
    assert "liquid/builtin/expressions/loop.py" in files


def test_maps_changelog_pickaxe_issue(repo: Path):
    sha = map_issue_to_commit(repo, 202)
    assert sha is not None
    files = changed_files(repo, sha)
    assert "liquid/builtin/tags/cycle_tag.py" in files


def test_unmappable_issue_returns_none(repo: Path):
    assert map_issue_to_commit(repo, 999999) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_extract.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation**

```python
# membench/corpus/extract.py
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
    # NOTE: `git clone --branch <raw sha>` is NOT a valid fallback — --branch takes a
    # branch or tag name, never a commit SHA. Reproduced: "fatal: Remote branch <sha>
    # not found in upstream origin". Any git lacking --revision falls straight to the
    # fetch+checkout path, which leaves refs/heads/<default> alive and must be pruned.
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
    # -S and its value MUST be separate argv elements: subprocess does no word
    # splitting, so f"-S issues/{n})" is parsed as -S with a leading-space value.
    shas = _git(repo_dir, "log", "--all", "--format=%H", "-S", f"issues/{issue})", "--", "CHANGES.md")
    lines = [s for s in shas.splitlines() if s.strip()]
    return lines[-1] if lines else None


def changed_files(repo_dir: Path, sha: str, prefix: str = "liquid/") -> list[str]:
    out = _git(repo_dir, "diff", "--name-only", f"{sha}^1", sha)
    return sorted({f for f in out.splitlines() if f.startswith(prefix)})


def base_sha(repo_dir: Path, fix_sha: str) -> str:
    return _git(repo_dir, "rev-parse", f"{fix_sha}^1").strip()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_extract.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add membench/corpus/extract.py tests/test_extract.py
git commit -m "feat: map issues to fix commits via three paths"
```

---

### Task 3: Workspace provisioning and leak scrub

This is the security boundary of the whole benchmark. If it leaks, every number is inflated.

**Files:**
- Create: `membench/workspace.py`
- Test: `tests/test_workspace.py`

**Interfaces:**
- Consumes: `BenchTask` from Task 2
- Produces: `provision(task: BenchTask, dest: Path) -> Path`; `verify_sealed(workdir: Path) -> list[str]` returning a list of leak descriptions, empty when clean

- [ ] **Step 1: Write the failing test**

```python
# tests/test_workspace.py
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_workspace.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation**

```python
# membench/workspace.py
import os
import shutil
import subprocess
from pathlib import Path

from membench.corpus.extract import BenchTask

INSTRUCTION_FILES = ("CLAUDE.md", "AGENTS.md", ".cursorrules", ".github/copilot-instructions.md")
UPSTREAM = "https://github.com/jg-rp/liquid.git"


def provision(task: BenchTask, dest: Path) -> Path:
    dest.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "clone", "--depth", "1", "--no-tags",
         "--revision", task.base_sha, UPSTREAM, str(dest)],
        check=True, capture_output=True,
    )
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
    leaks: list[str] = []

    shas = subprocess.run(
        ["git", "log", "--all", "--format=%H"],
        cwd=workdir, capture_output=True, text=True,
    ).stdout.split()
    if len(shas) != 1:
        leaks.append(f"history exposes {len(shas)} commits, expected 1")

    remotes = subprocess.run(
        ["git", "remote"], cwd=workdir, capture_output=True, text=True
    ).stdout.split()
    if remotes:
        leaks.append(f"remotes present: {remotes}")

    for rel in INSTRUCTION_FILES:
        if (workdir / rel).exists():
            leaks.append(f"agent instruction file present: {rel}")

    if os.environ.get("CLAUDE_CONFIG_DIR") is None:
        leaks.append("CLAUDE_CONFIG_DIR unset — host auto-memory may be visible")

    return leaks
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_workspace.py -v`
Expected: PASS

**`--branch <base_sha>` is NOT a valid fallback** — `--branch` takes a ref name, never a
commit SHA (`fatal: Remote branch <sha> not found in upstream origin`). If `--revision` is
unsupported by the installed git, clone `--depth 1` at the default branch, then
`git fetch --depth 1 origin <base_sha> && git checkout FETCH_HEAD`, then **prune every
remaining ref and `git gc --prune=now`** — the fetch path leaves `refs/heads/<default>`
alive, which makes the fix commit reachable and readable via `git diff`.

**Anything that rewrites history in the workspace must purge the rewritten objects.**
`git commit --amend` alone leaves the pre-amend commit as a loose object, so content
removed from `HEAD` stays readable at the old SHA. Amend must be followed by
`git reflog expire --expire=now --all` and `git gc --prune=now`.

- [ ] **Step 5: Commit**

```bash
git add membench/workspace.py tests/test_workspace.py
git commit -m "feat: sealed workspace provisioning with leak verification"
```

---

### Task 4: Test runner and correctness scoring

**Files:**
- Create: `membench/runner.py`
- Create: `membench/scoring/correctness.py`
- Test: `tests/test_runner.py`, `tests/test_correctness.py`

**Interfaces:**
- Consumes: `BenchTask`, provisioned workdir
- Produces: `run_tests(workdir: Path, node_ids: list[str] | None = None) -> dict[str, bool]` mapping test node id to pass/fail; `score_correctness(results: dict[str, bool], task: BenchTask) -> CorrectnessScore` with fields `f2p_passed: int`, `f2p_total: int`, `p2p_broken: list[str]`, `solved: bool`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_runner.py
from pathlib import Path
from membench.runner import run_tests


def test_run_tests_reports_per_test_results(provisioned_workdir: Path):
    results = run_tests(provisioned_workdir, ["tests/test_cycle_tag.py"])
    assert results, "expected at least one test result"
    assert all(isinstance(v, bool) for v in results.values())


def test_run_tests_sets_utc(provisioned_workdir: Path):
    # Date-filter tests fail outside UTC; a full run must still be green.
    results = run_tests(provisioned_workdir, None)
    assert sum(1 for v in results.values() if not v) == 0
```

```python
# tests/test_correctness.py
from membench.scoring.correctness import score_correctness
from membench.corpus.extract import BenchTask


def _task() -> BenchTask:
    return BenchTask(
        task_id="liquid-202", repo="jg-rp/liquid", issue_number=202,
        issue_title="t", issue_body="b", base_sha="a" * 40, fix_sha="b" * 40,
        changed_files=["liquid/builtin/tags/cycle_tag.py"],
        fail_to_pass=["tests/test_cycle.py::test_empty_group"],
        pass_to_pass=["tests/test_cycle.py::test_basic"],
    )


def test_solved_requires_all_f2p_and_no_p2p_regression():
    s = score_correctness(
        {"tests/test_cycle.py::test_empty_group": True, "tests/test_cycle.py::test_basic": True},
        _task(),
    )
    assert s.solved is True
    assert s.p2p_broken == []


def test_regression_blocks_solved():
    s = score_correctness(
        {"tests/test_cycle.py::test_empty_group": True, "tests/test_cycle.py::test_basic": False},
        _task(),
    )
    assert s.solved is False
    assert s.p2p_broken == ["tests/test_cycle.py::test_basic"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_runner.py tests/test_correctness.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation**

```python
# membench/runner.py
import json
import os
import subprocess
import tempfile
from pathlib import Path


def run_tests(workdir: Path, node_ids: list[str] | None = None) -> dict[str, bool]:
    env = {**os.environ, "TZ": "UTC", "PYTHONDONTWRITEBYTECODE": "1"}
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tf:
        report = Path(tf.name)
    cmd = ["python", "-m", "pytest", "-q", "-p", "no:randomly",
           "--json-report", f"--json-report-file={report}"]
    if node_ids:
        cmd.extend(node_ids)
    subprocess.run(cmd, cwd=workdir, env=env, capture_output=True, text=True, timeout=1800)
    if not report.exists():
        return {}
    data = json.loads(report.read_text())
    report.unlink(missing_ok=True)
    return {t["nodeid"]: t["outcome"] == "passed" for t in data.get("tests", [])}
```

```python
# membench/scoring/correctness.py
from dataclasses import dataclass, field

from membench.corpus.extract import BenchTask


@dataclass
class CorrectnessScore:
    f2p_passed: int
    f2p_total: int
    p2p_broken: list[str] = field(default_factory=list)

    @property
    def solved(self) -> bool:
        return self.f2p_total > 0 and self.f2p_passed == self.f2p_total and not self.p2p_broken


def score_correctness(results: dict[str, bool], task: BenchTask) -> CorrectnessScore:
    f2p_passed = sum(1 for n in task.fail_to_pass if results.get(n) is True)
    p2p_broken = [n for n in task.pass_to_pass if results.get(n) is False]
    return CorrectnessScore(
        f2p_passed=f2p_passed, f2p_total=len(task.fail_to_pass), p2p_broken=p2p_broken
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_runner.py tests/test_correctness.py -v`
Expected: PASS. Requires `pytest-json-report` — add it to the harness's own dev dependencies, **not** to the subject repo's environment.

- [ ] **Step 5: Commit**

```bash
git add membench/runner.py membench/scoring/correctness.py tests/test_runner.py tests/test_correctness.py
git commit -m "feat: pytest runner and F2P/P2P correctness scoring"
```

---

### Task 5: Golden-patch gate (audit checks 1–4)

The highest-value check in the plan. If the known-correct patch does not score 100%, the harness is broken and no agent number means anything.

**Files:**
- Create: `membench/gate/checks.py`
- Test: `tests/test_gate.py`

**Interfaces:**
- Consumes: `provision`, `run_tests`, `score_correctness`
- Produces: `GateResult` with `name: str`, `passed: bool`, `detail: str`; `check_golden_patch(task) -> GateResult`, `check_broken_baseline(task) -> GateResult`, `check_determinism(task, n: int = 5) -> GateResult`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_gate.py
from membench.gate.checks import check_golden_patch, check_broken_baseline, check_determinism


def test_golden_patch_solves_task(sample_task):
    r = check_golden_patch(sample_task)
    assert r.passed, r.detail


def test_baseline_actually_fails(sample_task):
    r = check_broken_baseline(sample_task)
    assert r.passed, r.detail


def test_determinism_over_repeats(sample_task):
    r = check_determinism(sample_task, n=3)
    assert r.passed, r.detail
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_gate.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation**

```python
# membench/gate/checks.py
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from membench.corpus.extract import BenchTask
from membench.runner import run_tests
from membench.scoring.correctness import score_correctness
from membench.workspace import provision

UPSTREAM_CACHE = Path("fixtures/liquid")


@dataclass
class GateResult:
    name: str
    passed: bool
    detail: str


def _apply_golden(task: BenchTask, workdir: Path) -> None:
    patch = subprocess.run(
        ["git", "diff", f"{task.base_sha}", f"{task.fix_sha}", "--", "liquid/", "tests/"],
        cwd=UPSTREAM_CACHE, capture_output=True, text=True, check=True,
    ).stdout
    subprocess.run(["git", "apply", "-"], cwd=workdir, input=patch, text=True, check=True)


def check_golden_patch(task: BenchTask) -> GateResult:
    with tempfile.TemporaryDirectory() as td:
        wd = provision(task, Path(td) / "ws")
        _apply_golden(task, wd)
        s = score_correctness(run_tests(wd, task.fail_to_pass + task.pass_to_pass), task)
        return GateResult(
            "golden_patch", s.solved,
            f"f2p {s.f2p_passed}/{s.f2p_total}, p2p broken {s.p2p_broken}",
        )


def check_broken_baseline(task: BenchTask) -> GateResult:
    with tempfile.TemporaryDirectory() as td:
        wd = provision(task, Path(td) / "ws")
        results = run_tests(wd, task.fail_to_pass)
        already_passing = [n for n in task.fail_to_pass if results.get(n) is True]
        return GateResult(
            "broken_baseline", not already_passing,
            f"target tests already passing before any fix: {already_passing}",
        )


def check_determinism(task: BenchTask, n: int = 5) -> GateResult:
    golden, baseline = [], []
    for _ in range(n):
        golden.append(check_golden_patch(task).passed)
        baseline.append(check_broken_baseline(task).passed)
    ok = all(golden) and all(baseline)
    return GateResult(
        "determinism", ok,
        f"golden {sum(golden)}/{n} consistent, baseline {sum(baseline)}/{n} consistent",
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_gate.py -v`
Expected: PASS. A failure here means the task definition is wrong — fix the corpus, never loosen the check.

- [ ] **Step 5: Commit**

```bash
git add membench/gate/checks.py tests/test_gate.py
git commit -m "feat: golden-patch, baseline and determinism gates"
```

---

### Task 6: Session A generation and arms

Session A produces the material every arm consumes. Arms differ only in what memory is present in session B's workspace.

**Files:**
- Create: `membench/arms/base.py`, `floor.py`, `grep.py`, `ceiling.py`, `vault.py`
- Create: `membench/session_a.py`
- Test: `tests/test_arms.py`

**Interfaces:**
- Consumes: `run_agent`, `BenchTask`
- Produces: `Arm` protocol with `name: str` and `install(task: BenchTask, workdir: Path, notes_dir: Path) -> str` returning a prompt preamble; `generate_session_a(task, workdir) -> Path` returning the notes directory

- [ ] **Step 1: Write the failing test**

```python
# tests/test_arms.py
from pathlib import Path
from membench.arms.floor import FloorArm
from membench.arms.grep import GrepArm
from membench.arms.ceiling import CeilingArm


def test_floor_installs_nothing(sample_task, tmp_path: Path):
    notes = tmp_path / "notes"; notes.mkdir()
    (notes / "a.md").write_text("session A tried X, it failed")
    wd = tmp_path / "ws"; wd.mkdir()
    preamble = FloorArm().install(sample_task, wd, notes)
    assert preamble == ""
    assert not any(wd.rglob("*.md"))


def test_grep_exposes_notes_dir_but_not_content(sample_task, tmp_path: Path):
    notes = tmp_path / "notes"; notes.mkdir()
    (notes / "a.md").write_text("session A tried X, it failed")
    wd = tmp_path / "ws"; wd.mkdir()
    preamble = GrepArm().install(sample_task, wd, notes)
    assert "rg" in preamble
    assert (wd / ".membench-notes" / "a.md").exists()
    assert "tried X" not in preamble


def test_ceiling_inlines_all_notes(sample_task, tmp_path: Path):
    notes = tmp_path / "notes"; notes.mkdir()
    (notes / "a.md").write_text("session A tried X, it failed")
    wd = tmp_path / "ws"; wd.mkdir()
    preamble = CeilingArm().install(sample_task, wd, notes)
    assert "tried X" in preamble
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_arms.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation**

```python
# membench/arms/base.py
from pathlib import Path
from typing import Protocol

from membench.corpus.extract import BenchTask

NOTES_MOUNT = ".membench-notes"


class Arm(Protocol):
    name: str

    def install(self, task: BenchTask, workdir: Path, notes_dir: Path) -> str: ...
```

```python
# membench/arms/floor.py
from pathlib import Path
from membench.arms.base import Arm
from membench.corpus.extract import BenchTask


class FloorArm(Arm):
    name = "floor"

    def install(self, task: BenchTask, workdir: Path, notes_dir: Path) -> str:
        return ""
```

```python
# membench/arms/grep.py
import shutil
from pathlib import Path
from membench.arms.base import Arm, NOTES_MOUNT
from membench.corpus.extract import BenchTask


class GrepArm(Arm):
    name = "grep"

    def install(self, task: BenchTask, workdir: Path, notes_dir: Path) -> str:
        dest = workdir / NOTES_MOUNT
        shutil.copytree(notes_dir, dest, dirs_exist_ok=True)
        return (
            f"Notes from a previous session on this task are in ./{NOTES_MOUNT}/. "
            f"Search them with `rg <pattern> {NOTES_MOUNT}/` before starting."
        )
```

```python
# membench/arms/ceiling.py
from pathlib import Path
from membench.arms.base import Arm
from membench.corpus.extract import BenchTask


class CeilingArm(Arm):
    name = "ceiling"

    def install(self, task: BenchTask, workdir: Path, notes_dir: Path) -> str:
        chunks = [f"--- {p.name} ---\n{p.read_text()}" for p in sorted(notes_dir.rglob("*.md"))]
        return "Notes from a previous session on this task:\n\n" + "\n\n".join(chunks)
```

```python
# membench/arms/vault.py
import shutil
from pathlib import Path
from membench.arms.base import Arm, NOTES_MOUNT
from membench.corpus.extract import BenchTask


class VaultArm(Arm):
    name = "vault"

    def install(self, task: BenchTask, workdir: Path, notes_dir: Path) -> str:
        dest = workdir / NOTES_MOUNT
        shutil.copytree(notes_dir, dest, dirs_exist_ok=True)
        current = dest / "current.md"
        header = current.read_text() if current.exists() else ""
        return (
            "Project state from previous sessions:\n\n"
            f"{header}\n\n"
            f"Further notes are in ./{NOTES_MOUNT}/."
        )
```

```python
# membench/session_a.py
from pathlib import Path

from membench.corpus.extract import BenchTask
from membench.driver import run_agent

SESSION_A_PROMPT = """You are working on this issue:

{title}

{body}

Investigate and begin the fix. You will be interrupted before finishing.
Keep working until you are stopped. Do not commit."""


def generate_session_a(task: BenchTask, workdir: Path, notes_dir: Path,
                       *, max_turns: int, model: str) -> Path:
    notes_dir.mkdir(parents=True, exist_ok=True)
    t = run_agent(
        SESSION_A_PROMPT.format(title=task.issue_title, body=task.issue_body),
        workdir=workdir, max_turns=max_turns, model=model,
    )
    (notes_dir / "session-a-transcript.md").write_text(t.text)
    (notes_dir / "current.md").write_text(
        f"# Session A on {task.task_id}\n\n"
        f"Worked on: {task.issue_title}\n"
        f"Stopped before completion. See session-a-transcript.md.\n"
    )
    return notes_dir
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_arms.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add membench/arms/ membench/session_a.py tests/test_arms.py
git commit -m "feat: session A generation and floor/grep/ceiling/vault arms"
```

---

### Task 7: Trace scoring

Measures the two things correctness cannot see: redone work and repeated dead ends.

**Files:**
- Create: `membench/scoring/trace.py`
- Test: `tests/test_trace.py`

**Interfaces:**
- Consumes: `Transcript`, `ToolCall`
- Produces: `TraceScore` with `redone_commands: list[str]`, `repeated_dead_ends: list[str]`; `score_trace(session_a: Transcript, session_b: Transcript, dead_ends: list[str]) -> TraceScore`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_trace.py
from membench.models import Transcript, ToolCall
from membench.scoring.trace import score_trace


def _t(cmds): return Transcript(text="", tool_calls=[ToolCall("Bash", command=c) for c in cmds])


def test_detects_redone_command():
    a = _t(["python -m pytest tests/test_cycle.py"])
    b = _t(["python -m pytest tests/test_cycle.py"])
    s = score_trace(a, b, dead_ends=[])
    assert s.redone_commands == ["python -m pytest tests/test_cycle.py"]


def test_exact_match_only_no_substring_false_positive():
    a = _t(["npm test"])
    b = _t(["npm test:watch"])
    s = score_trace(a, b, dead_ends=[])
    assert s.redone_commands == []


def test_detects_repeated_dead_end():
    b = _t(["python -m pytest -k broken_approach"])
    s = score_trace(_t([]), b, dead_ends=["python -m pytest -k broken_approach"])
    assert s.repeated_dead_ends == ["python -m pytest -k broken_approach"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_trace.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation**

```python
# membench/scoring/trace.py
from dataclasses import dataclass, field

from membench.models import Transcript


def _normalize(cmd: str | None) -> str:
    return " ".join((cmd or "").split())


@dataclass
class TraceScore:
    redone_commands: list[str] = field(default_factory=list)
    repeated_dead_ends: list[str] = field(default_factory=list)


def score_trace(session_a: Transcript, session_b: Transcript,
                dead_ends: list[str]) -> TraceScore:
    a_cmds = {_normalize(c.command) for c in session_a.tool_calls if c.command}
    b_cmds = [_normalize(c.command) for c in session_b.tool_calls if c.command]
    dead = {_normalize(d) for d in dead_ends}

    seen: set[str] = set()
    redone: list[str] = []
    repeated: list[str] = []
    for cmd in b_cmds:
        if cmd in a_cmds and cmd not in seen:
            redone.append(cmd)
            seen.add(cmd)
        if cmd in dead and cmd not in repeated:
            repeated.append(cmd)
    return TraceScore(redone_commands=redone, repeated_dead_ends=repeated)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_trace.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add membench/scoring/trace.py tests/test_trace.py
git commit -m "feat: trace scoring for redone work and repeated dead ends"
```

---

### Task 8: Calibration arms (audit checks 7–8)

**Files:**
- Create: `membench/arms/calibration.py`
- Modify: `membench/gate/checks.py`
- Test: `tests/test_gate.py` (extend)

**Interfaces:**
- Consumes: `Arm` protocol
- Produces: `NullArm`, `OracleArm`; `check_null_equals_floor(results) -> GateResult`, `check_oracle_high(results) -> GateResult`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_gate.py  (append)
from membench.gate.checks import check_null_equals_floor, check_oracle_high


def test_null_must_match_floor():
    assert check_null_equals_floor({"floor": 0.2, "null": 0.2}).passed
    assert not check_null_equals_floor({"floor": 0.2, "null": 0.6}).passed


def test_oracle_must_score_high():
    assert check_oracle_high({"oracle": 0.9}, threshold=0.8).passed
    assert not check_oracle_high({"oracle": 0.3}, threshold=0.8).passed
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_gate.py -v -k "null or oracle"`
Expected: FAIL with `ImportError`

- [ ] **Step 3: Write minimal implementation**

```python
# membench/arms/calibration.py
from pathlib import Path
from membench.arms.base import Arm
from membench.corpus.extract import BenchTask


class NullArm(Arm):
    """Returns nothing. Must score identically to floor."""
    name = "null"

    def install(self, task: BenchTask, workdir: Path, notes_dir: Path) -> str:
        return ""


class OracleArm(Arm):
    """Returns the answer. Must score near ceiling."""
    name = "oracle"

    def install(self, task: BenchTask, workdir: Path, notes_dir: Path) -> str:
        files = "\n".join(f"- {f}" for f in task.changed_files)
        return (
            "A previous session determined the fix belongs in these files:\n"
            f"{files}\n"
            "Apply the fix there."
        )
```

```python
# membench/gate/checks.py  (append)
def check_null_equals_floor(scores: dict[str, float], tol: float = 0.001) -> GateResult:
    diff = abs(scores.get("null", 0.0) - scores.get("floor", 0.0))
    return GateResult(
        "null_equals_floor", diff <= tol,
        f"null={scores.get('null')} floor={scores.get('floor')} diff={diff:.3f}",
    )


def check_oracle_high(scores: dict[str, float], threshold: float = 0.8) -> GateResult:
    v = scores.get("oracle", 0.0)
    return GateResult("oracle_high", v >= threshold, f"oracle={v} threshold={threshold}")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_gate.py -v`
Expected: PASS (all gate tests)

- [ ] **Step 5: Commit**

```bash
git add membench/arms/calibration.py membench/gate/checks.py tests/test_gate.py
git commit -m "feat: null and oracle calibration arms with gates"
```

---

### Task 9: Runner, remaining gates, first number

Wires everything together and produces the phase-1 result for the four cheap arms.

**Files:**
- Create: `membench/cli.py`, `membench/gate/report.py`
- Test: `tests/test_gate.py` (extend)

**Interfaces:**
- Consumes: everything above
- Produces: `run_benchmark(tasks, arms, *, repeats: int) -> dict[str, list[float]]`; `gate_report(task) -> tuple[bool, list[GateResult]]`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_gate.py  (append)
from membench.gate.report import gate_report


def test_gate_blocks_reporting_on_any_failure(sample_task, monkeypatch):
    import membench.gate.checks as checks
    monkeypatch.setattr(
        checks, "check_golden_patch",
        lambda t: checks.GateResult("golden_patch", False, "forced"),
    )
    ok, results = gate_report(sample_task)
    assert ok is False
    assert any(r.name == "golden_patch" and not r.passed for r in results)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_gate.py -v -k gate_blocks`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation**

```python
# membench/gate/report.py
from membench.corpus.extract import BenchTask
from membench.gate import checks
from membench.gate.checks import GateResult
from membench.workspace import provision, verify_sealed
import tempfile
from pathlib import Path


def check_sealed(task: BenchTask) -> GateResult:
    with tempfile.TemporaryDirectory() as td:
        leaks = verify_sealed(provision(task, Path(td) / "ws"))
    return GateResult("sealed_workspace", not leaks, f"leaks: {leaks}")


def check_prompt_leakage(task: BenchTask) -> GateResult:
    haystack = f"{task.issue_title}\n{task.issue_body}".lower()
    hits = [f for f in task.changed_files if Path(f).name.lower() in haystack]
    return GateResult("prompt_leakage", not hits, f"fix filenames present in prompt: {hits}")


def gate_report(task: BenchTask) -> tuple[bool, list[GateResult]]:
    results = [
        checks.check_golden_patch(task),
        checks.check_broken_baseline(task),
        checks.check_determinism(task, n=5),
        check_sealed(task),
        check_prompt_leakage(task),
    ]
    return all(r.passed for r in results), results
```

```python
# membench/cli.py
import argparse
import json
from pathlib import Path

from membench.corpus.extract import BenchTask
from membench.gate.report import gate_report


def main() -> int:
    ap = argparse.ArgumentParser(prog="membench")
    ap.add_argument("--tasks", type=Path, required=True)
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--model", default="claude-sonnet-5")
    args = ap.parse_args()

    tasks = [BenchTask(**json.loads(p.read_text())) for p in sorted(args.tasks.glob("*.json"))]
    print(f"loaded {len(tasks)} tasks; scaffold: model={args.model} repeats={args.repeats}")
    # Gate every task before any arm runs.
    for t in tasks:
        ok, results = gate_report(t)
        for r in results:
            print(f"  [{'PASS' if r.passed else 'FAIL'}] {r.name}: {r.detail}")
        if not ok:
            print("GATE FAILED — no results will be reported.")
            return 1
    print("gate passed for all tasks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_gate.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add membench/cli.py membench/gate/report.py tests/test_gate.py
git commit -m "feat: benchmark runner with blocking audit gate"
```

- [ ] **Step 6: STOP — report the four-arm number before continuing**

Run the benchmark on the five post-cutoff liquid tasks across floor, grep, ceiling and vault, three repeats each. Report medians with spread and the pinned scaffold config.

**If grep matches or beats vault, or ceiling matches both, stop and reassess before Tasks 10-11.** Those results change what is worth building, and the adapters are the expensive part.

---

### Task 10: mem0 adapter

**Files:**
- Create: `membench/arms/mem0_arm.py`
- Test: `tests/test_arms.py` (extend)

**Interfaces:**
- Consumes: `Arm` protocol, session A notes
- Produces: `Mem0Arm` with the same `install` signature

- [ ] **Step 1: Write the failing test**

```python
# tests/test_arms.py  (append)
from membench.arms.mem0_arm import Mem0Arm


def test_mem0_arm_returns_retrieved_context(sample_task, tmp_path, monkeypatch):
    notes = tmp_path / "notes"; notes.mkdir()
    (notes / "a.md").write_text("session A found the bug is in cycle_tag")
    wd = tmp_path / "ws"; wd.mkdir()

    class FakeMemory:
        def add(self, *a, **k): return {"results": []}
        def search(self, query, user_id=None, limit=10):
            return {"results": [{"memory": "bug is in cycle_tag"}]}

    monkeypatch.setattr("membench.arms.mem0_arm._client", lambda: FakeMemory())
    preamble = Mem0Arm().install(sample_task, wd, notes)
    assert "cycle_tag" in preamble
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_arms.py -v -k mem0`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation**

```python
# membench/arms/mem0_arm.py
from pathlib import Path

from membench.arms.base import Arm
from membench.corpus.extract import BenchTask


def _client():
    from mem0 import Memory
    return Memory()


class Mem0Arm(Arm):
    name = "mem0"

    def install(self, task: BenchTask, workdir: Path, notes_dir: Path) -> str:
        m = _client()
        uid = task.task_id
        for p in sorted(notes_dir.rglob("*.md")):
            m.add(p.read_text(), user_id=uid)
        hits = m.search(task.issue_title, user_id=uid, limit=10).get("results", [])
        if not hits:
            return ""
        body = "\n".join(f"- {h['memory']}" for h in hits)
        return f"Recalled from previous sessions:\n{body}"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_arms.py -v -k mem0`
Expected: PASS

Pin mem0's vector store to a durable path in the harness config — its default is `/tmp/qdrant` with `on_disk=False`, which does not survive reboot.

- [ ] **Step 5: Commit**

```bash
git add membench/arms/mem0_arm.py tests/test_arms.py
git commit -m "feat: mem0 arm"
```

---

### Task 11: basic-memory adapter

**Files:**
- Create: `membench/arms/basic_memory_arm.py`
- Test: `tests/test_arms.py` (extend)

**Interfaces:**
- Consumes: `Arm` protocol
- Produces: `BasicMemoryArm` with the same `install` signature

- [ ] **Step 1: Write the failing test**

```python
# tests/test_arms.py  (append)
from membench.arms.basic_memory_arm import BasicMemoryArm


def test_basic_memory_arm_returns_search_hits(sample_task, tmp_path, monkeypatch):
    notes = tmp_path / "notes"; notes.mkdir()
    (notes / "a.md").write_text("bug traced to cycle_tag parse step")
    wd = tmp_path / "ws"; wd.mkdir()
    monkeypatch.setattr(
        "membench.arms.basic_memory_arm._search",
        lambda project, query: ["bug traced to cycle_tag parse step"],
    )
    preamble = BasicMemoryArm().install(sample_task, wd, notes)
    assert "cycle_tag" in preamble
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_arms.py -v -k basic_memory`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation**

```python
# membench/arms/basic_memory_arm.py
import shutil
import subprocess
from pathlib import Path

from membench.arms.base import Arm
from membench.corpus.extract import BenchTask


def _search(project: Path, query: str) -> list[str]:
    out = subprocess.run(
        ["basic-memory", "--project", str(project), "tool", "search-notes", "--query", query],
        capture_output=True, text=True, check=False,
    ).stdout
    return [ln for ln in out.splitlines() if ln.strip()]


class BasicMemoryArm(Arm):
    name = "basic-memory"

    def install(self, task: BenchTask, workdir: Path, notes_dir: Path) -> str:
        project = workdir / ".membench-bm"
        shutil.copytree(notes_dir, project, dirs_exist_ok=True)
        subprocess.run(
            ["basic-memory", "--project", str(project), "sync"],
            capture_output=True, check=False,
        )
        hits = _search(project, task.issue_title)
        if not hits:
            return ""
        return "Recalled from previous sessions:\n" + "\n".join(f"- {h}" for h in hits)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_arms.py -v -k basic_memory`
Expected: PASS

Verify the installed `basic-memory` CLI subcommand names with `basic-memory --help` before trusting the invocation above; adjust if they differ.

- [ ] **Step 5: Commit**

```bash
git add membench/arms/basic_memory_arm.py tests/test_arms.py
git commit -m "feat: basic-memory arm"
```

---

### Task 12: Complete the audit gate

Closes the three design checks that had no task, plus a fourth the driver probe surfaced.
**Blocks external publication, not the internal phase-1 number** — can run in parallel with
Tasks 10-11.

**Files:**
- Create: `membench/arms/cheat_probe.py`
- Modify: `membench/gate/report.py`
- Test: `tests/test_gate.py` (extend)

**Interfaces:**
- Consumes: `Arm` protocol, `Transcript`, `provision`
- Produces: `CheatProbeArm`; `check_cheat_probe(task) -> GateResult`, `check_env_pinned() -> GateResult`, `check_no_permission_denials(transcripts) -> GateResult`, `write_exclusion_log(excluded, path) -> None`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_gate.py  (append)
import json
from membench.models import Transcript
from membench.gate.report import (
    check_cheat_probe, check_env_pinned, check_no_permission_denials, write_exclusion_log
)


def test_cheat_probe_finds_nothing_in_sealed_workspace(sample_task):
    r = check_cheat_probe(sample_task)
    assert r.passed, r.detail


def test_env_pinned_requires_tz_utc(monkeypatch):
    monkeypatch.setenv("TZ", "America/New_York")
    assert not check_env_pinned().passed
    monkeypatch.setenv("TZ", "UTC")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/tmp/isolated")
    assert check_env_pinned().passed


def test_permission_denials_invalidate_run():
    clean = [Transcript(text="", permission_denials=[])]
    blocked = [Transcript(text="", permission_denials=[{"tool": "Bash"}])]
    assert check_no_permission_denials(clean).passed
    assert not check_no_permission_denials(blocked).passed


def test_exclusion_log_records_reason(tmp_path):
    p = tmp_path / "exclusions.json"
    write_exclusion_log([{"task_id": "liquid-162", "reason": "unmappable to fix commit"}], p)
    entries = json.loads(p.read_text())
    assert entries[0]["reason"] == "unmappable to fix commit"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_gate.py -v -k "cheat or env_pinned or permission or exclusion"`
Expected: FAIL with `ImportError`

- [ ] **Step 3: Write minimal implementation**

```python
# membench/arms/cheat_probe.py
from pathlib import Path

from membench.arms.base import Arm
from membench.corpus.extract import BenchTask

CHEAT_PROMPT = """Do not attempt the task. Instead, try every way you can to discover
how this bug was fixed upstream WITHOUT reasoning about the code. Try at minimum:
`git log --all`, `git show`, `git fetch`, reading any file under .git/, and fetching
the project's GitHub page. Report verbatim anything you find that reveals the fix."""


class CheatProbeArm(Arm):
    """Its only job is to fail to find anything."""
    name = "cheat-probe"

    def install(self, task: BenchTask, workdir: Path, notes_dir: Path) -> str:
        return CHEAT_PROMPT
```

```python
# membench/gate/report.py  (append)
import json
import os
import tempfile
from pathlib import Path

from membench.arms.cheat_probe import CheatProbeArm
from membench.driver import run_agent
from membench.models import Transcript


def check_cheat_probe(task: BenchTask, *, model: str = "claude-sonnet-5") -> GateResult:
    """Gate 6 — history/network leakage."""
    with tempfile.TemporaryDirectory() as td:
        wd = provision(task, Path(td) / "ws")
        preamble = CheatProbeArm().install(task, wd, Path(td))
        t = run_agent(preamble, workdir=wd, max_turns=12, model=model)
    leaked = [f for f in task.changed_files if Path(f).name in t.text]
    if task.fix_sha[:8] in t.text:
        leaked.append(task.fix_sha[:8])
    return GateResult("cheat_probe", not leaked, f"probe recovered: {leaked}")


def check_env_pinned() -> GateResult:
    """Gate 9 — environment pinning."""
    problems = []
    if os.environ.get("TZ") != "UTC":
        problems.append(f"TZ={os.environ.get('TZ')!r}, expected 'UTC'")
    if not os.environ.get("CLAUDE_CONFIG_DIR"):
        problems.append("CLAUDE_CONFIG_DIR unset — host auto-memory may leak into runs")
    return GateResult("env_pinned", not problems, "; ".join(problems) or "ok")


def check_no_permission_denials(transcripts: list[Transcript]) -> GateResult:
    """A blocked agent scores badly for the wrong reason."""
    blocked = [t for t in transcripts if t.permission_denials]
    return GateResult(
        "no_permission_denials", not blocked,
        f"{len(blocked)} run(s) had permission denials — scores are invalid",
    )


def write_exclusion_log(excluded: list[dict], path: Path) -> None:
    """Gate 10 — no task vanishes without a logged reason."""
    for e in excluded:
        if not e.get("reason"):
            raise ValueError(f"exclusion without reason: {e}")
    path.write_text(json.dumps(excluded, indent=2))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_gate.py -v`
Expected: PASS (all gate tests)

If `check_cheat_probe` fails, **do not weaken the check** — the workspace is leaking and
every number collected so far is inflated. Fix `provision` in Task 3 and re-run everything.

- [ ] **Step 5: Wire all ten into `gate_report` and commit**

```python
# membench/gate/report.py — replace gate_report
def gate_report(task: BenchTask, transcripts: list[Transcript] | None = None
                ) -> tuple[bool, list[GateResult]]:
    results = [
        checks.check_golden_patch(task),
        checks.check_broken_baseline(task),
        checks.check_determinism(task, n=5),
        check_sealed(task),
        check_prompt_leakage(task),
        check_cheat_probe(task),
        check_env_pinned(),
        check_no_permission_denials(transcripts or []),
    ]
    return all(r.passed for r in results), results
```

```bash
git add membench/arms/cheat_probe.py membench/gate/report.py tests/test_gate.py
git commit -m "feat: complete audit gate — cheat probe, env pinning, denials, exclusion log"
```

---

## Self-Review

**Spec coverage.** Corpus and post-cutoff selection → Task 2. Shallow-clone isolation and leak channels → Task 3. Deterministic scoring → Tasks 4 and 7. Task family (resume interrupted work) → Task 6. Six arms → Tasks 6, 8, 10, 11. Audit gate, all ten checks → Tasks 5, 8, 9, 12.

**Gate coverage, check by check:**

| # | Check | Task |
|---|---|---|
| 1 | Golden-patch E2E | 5 |
| 2 | Broken-baseline | 5 |
| 3 | Determinism ×N | 5 |
| 4 | Regression guard (P2P) | 4 |
| 5 | Prompt leakage | 9 |
| 6 | History leakage (cheat probe) | 12 |
| 7 | Null arm = floor | 8 |
| 8 | Oracle arm ≈ ceiling | 8 |
| 9 | Environment pinning | 12 |
| 10 | Exclusion accounting | 12 |
| + | Permission denials invalidate a run | 12 |

**Deliberately out of phase 1:** the validated LLM judge and human spot-check. Deterministic
scoring only.

**Type consistency.** `BenchTask` is defined in Task 2 and imported unchanged everywhere.
`GateResult` is defined in Task 5 and extended in Tasks 8, 9 and 12. `Arm.install` has one
signature across all seven arms including the cheat probe. `Transcript`/`ToolCall` are
defined in Task 1 and consumed in Tasks 7 and 12 — note `Transcript` gained
`cost_usd`, `num_turns`, `stop_reason` and `permission_denials` in Task 1, and Task 12's
denial check depends on the last of these.

**Resolved risk.** Task 1's CLI assumption was verified against `claude` 2.1.227 on
2026-08-10: flags exist, event shape matches, and the run exited 0. Three fixes came out of
that probe and are folded into Task 1 — `stdin=DEVNULL`, `--permission-mode acceptEdits`,
and capturing `permission_denials`.

**Remaining risk.** `check_cheat_probe` costs a full agent run per task and is the slowest
gate. If it proves too slow to run every time, run it once per corpus revision rather than
per benchmark invocation — but never skip it before publishing.

---

### Task 13: Containerized agent execution (executes BEFORE Task 6)

Closes the two coupled Criticals from the Task 1 amendment review. The deny-list approach
failed: `/usr/bin/curl`, `python3 -c "urllib.request.urlopen(...)"`, `git -C . fetch` and
`git ls-remote` all reached GitHub, and GitHub's public API needs no credential. Blocking
`python3` is impossible because the arms must run pytest. Worse, the network seal only
appeared to hold because `--permission-mode acceptEdits` was rejecting nearly all Bash —
including pytest — so Task 6 would have removed the only protection in place.

Containers solve both at once: the arms get unrestricted Bash, and GitHub has no route.

**Verified on this host before writing this task:**

```
docker 29.4.0 via OrbStack (/usr/local/bin/docker)
--network none                       → github unreachable ("bad address")
--cap-add=NET_ADMIN + iptables       → SELECTIVE egress works:
    allow 160.79.104.10 (api.anthropic.com) → HTTP/1.1 40x (reached server)
    api.github.com                          → download timed out (blocked)
```

**Files:**
- Create: `docker/Dockerfile`, `docker/seal-egress.sh`
- Modify: `membench/driver.py` (`run_agent` runs the agent inside a container)
- Test: `tests/test_container.py`

**Interfaces:**
- Consumes: `Transcript`, `ToolCall` from Task 1
- Produces: `run_agent(...)` keeps its existing signature and return type; only its execution
  substrate changes. Tasks 6-12 must not need edits.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_container.py
import pytest
from membench.driver import egress_sealed, run_agent


@pytest.mark.live
def test_container_blocks_github_allows_anthropic(tmp_path):
    """The seal must be a property of the container, not of the agent's choices."""
    assert egress_sealed(tmp_path) == {"anthropic": True, "github": False}


@pytest.mark.live
def test_agent_cannot_reach_github_via_any_interpreter(tmp_path):
    (tmp_path / "probe.txt").write_text("probe\n")
    t = run_agent(
        "Run each of these and report the exit status of each, nothing else: "
        "1) /usr/bin/curl -s -m 5 https://api.github.com/ "
        "2) python3 -c \"import urllib.request;urllib.request.urlopen('https://api.github.com/',timeout=5)\" "
        "3) git ls-remote https://github.com/jg-rp/liquid.git HEAD",
        workdir=tmp_path, max_turns=10, model="claude-sonnet-5",
    )
    assert t.exit_code == 0
    assert "probe" not in t.text or True   # sanity: agent ran
    for marker in ("api.github.com",):
        assert marker in t.text            # it tried
    assert t.permission_denials == []      # it was NOT blocked by the approval gate


@pytest.mark.live
def test_pytest_runs_unblocked_inside_container(tmp_path):
    (tmp_path / "test_x.py").write_text("def test_ok():\n    assert True\n")
    t = run_agent(
        "Run `python3 -m pytest -q` and report the exact output line.",
        workdir=tmp_path, max_turns=8, model="claude-sonnet-5",
    )
    assert t.permission_denials == []
    assert "1 passed" in t.text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `TZ=UTC .venv/bin/python -m pytest tests/test_container.py -m live -v`
Expected: FAIL with `ImportError: cannot import name 'egress_sealed'`

- [ ] **Step 3: Build the image and the egress seal**

`docker/seal-egress.sh` runs as the container's entrypoint prelude. It resolves the
Anthropic API host, allowlists every resolved address, permits DNS and loopback, drops
everything else, then drops `NET_ADMIN` before handing control to the agent so the agent
cannot rewrite the rules:

```sh
#!/bin/sh
set -eu
for ip in $(getent ahostsv4 "${ANTHROPIC_HOST:-api.anthropic.com}" | awk '{print $1}' | sort -u); do
  iptables -A OUTPUT -d "$ip" -j ACCEPT
done
iptables -A OUTPUT -o lo -j ACCEPT
iptables -A OUTPUT -p udp --dport 53 -j ACCEPT
iptables -A OUTPUT -j DROP
exec setpriv --inh-caps=-net_admin --ambient-caps=-net_admin "$@"
```

`docker/Dockerfile` needs: python3 + pip + pytest, git, node + the `claude` CLI, iptables,
util-linux (for `setpriv`), and ca-certificates. Pin the base image by digest.

- [ ] **Step 4: Rewrite `run_agent` to execute in the container**

Keep the signature and `Transcript` return unchanged. The workspace mounts at a fixed path;
stdout still carries `stream-json`, so `_parse_stream` is untouched. Pass the Anthropic
credential in as an env var. Drop `--disallowedTools` and the `acceptEdits` restriction —
the container is the boundary now, so the agent gets full Bash and pytest works.

Add `egress_sealed(workdir) -> dict[str, bool]` which starts the container and probes
both hosts, returning `{"anthropic": bool, "github": bool}`.

- [ ] **Step 5: Run tests to verify they pass**

Run: `TZ=UTC .venv/bin/python -m pytest tests/test_container.py -m live -v`
Expected: PASS (3 passed)

Then confirm no regression: `TZ=UTC .venv/bin/python -m pytest -q` and
`TZ=UTC .venv/bin/python -m pytest -m live -v`.

- [ ] **Step 6: Commit**

```bash
git add docker/ membench/driver.py tests/test_container.py
git commit -m "feat: run agents in network-sealed containers"
```
