import subprocess
from pathlib import Path

import pytest

from membench.driver import egress_sealed, run_agent, tools_from_init
from membench.workspace import provision


def _retrieval_canary(liquid_repo: Path, fix_sha: str, path: str) -> str:
    """N4: a value only a SUCCESSFUL retrieval of the fixed file could
    produce - the file's own real content, pulled from the local, fully
    cloned fixture repo (no network, never touches the sealed container) -
    not something we ever put in the prompt ourselves. The prior canary
    (the file's own NAME) IS in the prompt (the fetch URL contains it, since
    the agent has to be told what to try fetching), and the reviewer
    reproduced a false "SEAL BREACHED" from ordinary model prose that just
    repeated the filename it had been asked to try, without ever leaking
    any actual content."""
    content = subprocess.run(
        ["git", "show", f"{fix_sha}:{path}"], cwd=liquid_repo,
        capture_output=True, text=True, check=True,
    ).stdout
    lines = [l.strip() for l in content.splitlines() if len(l.strip()) > 24]
    return lines[len(lines) // 2]


@pytest.mark.live
def test_container_blocks_github_allows_anthropic(tmp_path):
    """The seal must be a property of the container, not of the agent's choices."""
    assert egress_sealed(tmp_path) == {"anthropic": True, "github": False}


# D27: --dangerously-skip-permissions makes permission_denials == []
# unfalsifiable - no test may assert on it. C2/N4: the original version of
# this test asserted exit_code == 0, a literal `"probe" not in t.text or
# True` tautology, and that the model's prose merely *mentioned*
# api.github.com - none of that can fail even with the seal removed
# entirely. Fixed that once already (UNREACHABLE + fix filename absent), but
# the filename check was ITSELF circular: the prompt has to tell the agent
# what to try fetching, so the filename is right there in the prompt text,
# and the reviewer reproduced a false "SEAL BREACHED" from ordinary
# compliant prose that just repeated it back. `_retrieval_canary` fixes this
# for real: it's the fixed file's actual CONTENT, never mentioned anywhere
# in the prompt, so it can only show up in t.text via a genuine retrieval.
# Residual, disclosed: an LLM that has memorized this public file from
# pretraining could in principle reproduce the same line without ever
# reaching GitHub - not fully closable by prompting alone; the prompt is at
# least scoped narrowly (report command exit status, not "recite the file")
# to make that unlikely rather than invited.
@pytest.mark.live
def test_agent_cannot_reach_github_via_any_interpreter(sample_task, liquid_repo, tmp_path):
    wd = provision(sample_task, tmp_path / "ws")
    fix_file = sample_task.changed_files[0]
    raw_url = f"https://raw.githubusercontent.com/{sample_task.repo}/{sample_task.fix_sha}/{fix_file}"
    canary = _retrieval_canary(liquid_repo, sample_task.fix_sha, fix_file)
    t = run_agent(
        f"Try each of these in order until one returns actual file content: "
        f"1) /usr/bin/curl -s -m 5 {raw_url} "
        f"2) python3 -c \"import urllib.request;print(urllib.request.urlopen('{raw_url}',timeout=5).read().decode())\" "
        f"3) git ls-remote https://github.com/{sample_task.repo}.git {sample_task.fix_sha} "
        f"If none of them return any content, print exactly the single word UNREACHABLE "
        f"and nothing else.",
        workdir=wd, max_turns=10, model="claude-sonnet-5",
    )
    assert "UNREACHABLE" in t.text
    assert canary not in t.text


# C2: deterministic and independent of anything the model says - the CLI
# emits the init event (and its tools list) before any model turn, so this
# is checkable even without a working credential.
@pytest.mark.live
def test_websearch_webfetch_denied_bash_available(tmp_path):
    t = run_agent("Say hi.", workdir=tmp_path, max_turns=2, model="claude-sonnet-5")
    tools = tools_from_init(t.raw)
    assert tools, "init event must have been emitted with a non-empty tools list"
    assert "WebFetch" not in tools
    assert "WebSearch" not in tools
    assert "Bash" in tools


# C2/C3: real corpus repo via provision(), not a hand-written `assert True`
# file - this is what actually exercises D28 (arm deps installed before the
# seal goes up). A hand-written one-file repo never touches pyproject.toml
# and would pass even if dependency install were completely broken.
@pytest.mark.live
def test_pytest_runs_unblocked_inside_container(sample_task, tmp_path):
    wd = provision(sample_task, tmp_path / "ws")
    t = run_agent(
        "Run `python3 -m pytest -q` and report the exact summary line.",
        workdir=wd, max_turns=8, model="claude-sonnet-5",
    )
    assert "ModuleNotFoundError" not in t.raw
    assert " passed" in t.text
