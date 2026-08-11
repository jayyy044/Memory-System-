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
