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


# The tool-layer deny-list this test used to exercise (--disallowedTools
# "Bash(gh *),Bash(curl *),Bash(git fetch*)") was removed under Task 13/D22:
# it was defeated four ways in one review (absolute-path curl, python3
# urllib, git -C fetch, git ls-remote), and gh isn't even installed in the
# sealed image. The seal is now a network-layer property of the container,
# not a tool-layer property of the agent's choices - see
# tests/test_container.py::test_agent_cannot_reach_github_via_any_interpreter
# and ::test_container_blocks_github_allows_anthropic for the replacement
# coverage.
