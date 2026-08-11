import json
import subprocess
from pathlib import Path

import pytest

from membench.driver import egress_sealed, run_agent, tools_from_init
from membench.workspace import provision


def _retrieval_canary(liquid_repo: Path, base_sha: str, fix_sha: str, path: str) -> str:
    """A value only a SUCCESSFUL retrieval of the FIXED file could produce:
    the longest line the fix ADDS, read from the local fixture clone (no
    network, never touches the sealed container).

    Two prior versions of this were unfalsifiable. v1 was the file's own
    NAME, which the prompt necessarily contains (it's in the fetch URL), so
    compliant prose repeating it back read as a breach. v2 was a line of the
    fixed file's content chosen from the whole file - but the workspace is
    checked out at base_sha, so most such lines are ALSO in the local tree
    and would fire on a plain `cat`, and D39: it did not fire on a real
    breach either. Restricting to added-only lines makes a hit mean "this
    text exists only in the post-fix blob, which is only on GitHub"."""
    diff = subprocess.run(
        ["git", "diff", "--unified=0", base_sha, fix_sha, "--", path], cwd=liquid_repo,
        capture_output=True, text=True, check=True,
    ).stdout
    added = [
        l[1:].strip() for l in diff.splitlines()
        if l.startswith("+") and not l.startswith("+++") and len(l.strip()) > 24
    ]
    assert added, f"fix {fix_sha} must add at least one substantial line to {path}"
    return max(added, key=len)


def _stream_strings(raw: str) -> str:
    """Every decoded string in the stream-json output, TOOL RESULTS
    included. D39: the canary was checked against `t.text`, which
    _parse_stream builds from assistant text/result blocks only - so in a
    deliberate-unseal run the agent DID fetch the file and the canary still
    never appeared, because retrieved bytes arrive in a tool_result the
    model then summarizes rather than quotes. This is the channel a
    successful retrieval must pass through, and JSON-decoding it (rather
    than substring-matching the raw line) is what makes content containing
    quotes or backslashes matchable at all."""
    found: list[str] = []

    def walk(o):
        if isinstance(o, str):
            found.append(o)
        elif isinstance(o, dict):
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            walk(json.loads(line))
        except json.JSONDecodeError:
            continue
    return "\n".join(found)


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
# compliant prose that just repeated it back.
#
# D39: the round-2 canary then failed the OTHER way - a false negative. In a
# deliberate unseal the agent DID reach GitHub and it still did not fire,
# because it searched `t.text` (assistant prose only) for bytes that arrive
# in a tool_result. Both halves are fixed here: `_stream_strings` searches
# the channel a retrieval actually uses, and `_retrieval_canary` returns a
# line that exists only in the post-fix blob, so a local `cat` of the
# base_sha checkout in the workspace cannot trip it either. Demonstrated to
# fire, not assumed: a production run_agent call whose Bash tool read that
# same content from a local file put the canary in _stream_strings(t.raw) -
# and, confirming exactly the bug D39 describes, NOT in t.text.
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
    canary = _retrieval_canary(liquid_repo, sample_task.base_sha, sample_task.fix_sha, fix_file)
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
    assert canary not in _stream_strings(t.raw)


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
