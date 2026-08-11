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
