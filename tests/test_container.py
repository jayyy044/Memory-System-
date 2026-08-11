from pathlib import Path

import pytest

from membench.driver import egress_sealed, run_agent, tools_from_init
from membench.workspace import provision


@pytest.mark.live
def test_container_blocks_github_allows_anthropic(tmp_path):
    """The seal must be a property of the container, not of the agent's choices."""
    assert egress_sealed(tmp_path) == {"anthropic": True, "github": False}


# D27: --dangerously-skip-permissions makes permission_denials == []
# unfalsifiable - no test may assert on it. C2: the previous version of this
# test asserted exit_code == 0, a literal `"probe" not in t.text or True`
# tautology, and that the model's prose merely *mentioned* api.github.com -
# none of that can fail even with the seal removed entirely. This version
# asks for a specific outcome (the literal word UNREACHABLE, nothing else on
# success) and checks the one thing a leak would actually produce: the fixed
# file's real name showing up in the model's own reply. The prompt itself
# necessarily contains that name (it's in the commands we ask the agent to
# run), but ToolCall.command text never reaches Transcript.text - only
# `text`/`result` blocks do (see _parse_stream) - so this is not circular.
@pytest.mark.live
def test_agent_cannot_reach_github_via_any_interpreter(sample_task, tmp_path):
    wd = provision(sample_task, tmp_path / "ws")
    fix_file = sample_task.changed_files[0]
    fix_filename = Path(fix_file).name
    raw_url = f"https://raw.githubusercontent.com/{sample_task.repo}/{sample_task.fix_sha}/{fix_file}"
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
    assert fix_filename not in t.text


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
