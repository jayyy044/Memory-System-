from pathlib import Path
import pytest
from membench.driver import run_agent, Transcript


@pytest.mark.live
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


@pytest.mark.live
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


@pytest.mark.live
def test_run_agent_seals_network_and_gh(tmp_path: Path):
    """The child must not be able to retrieve the answer via gh or fetch the
    web, even though gh is installed and authenticated on this host and the
    host has real network access. Both attempts must be blocked at the tool
    layer, not merely fail for some unrelated reason."""
    t = run_agent(
        "Attempt both of these and report exactly what happened for each, "
        "do not stop if one fails: "
        "1) run `gh api repos/jg-rp/liquid/commits --jq '.[0].sha'` via Bash "
        "2) use the WebFetch tool to fetch https://example.com",
        workdir=tmp_path,
        max_turns=6,
        model="claude-sonnet-5",
    )
    assert t.exit_code == 0
    denied_tools = {d.get("tool_name") for d in t.permission_denials}
    denied_commands = " ".join(
        d.get("tool_input", {}).get("command", "") for d in t.permission_denials
    )
    assert "Bash" in denied_tools
    assert "gh api" in denied_commands
    assert "WebFetch" not in [c.name for c in t.tool_calls]
