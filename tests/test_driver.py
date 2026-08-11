from pathlib import Path
import pytest
from membench.driver import _PASSTHROUGH_ENV_VARS, _container_env_args, run_agent, Transcript


# D37/D38: no docker needed - these are the two credential-handling
# regressions in pure argument construction, so they run in the default
# suite rather than only under -m live.
def test_credential_never_appears_in_argv(monkeypatch):
    monkeypatch.setenv(_PASSTHROUGH_ENV_VARS[0], "sk-secret-token-value")
    args, secret_env = _container_env_args(credentials=True)
    assert "sk-secret-token-value" not in " ".join(args)
    assert _PASSTHROUGH_ENV_VARS[0] in args  # bare `-e NAME` form
    assert secret_env[_PASSTHROUGH_ENV_VARS[0]] == "sk-secret-token-value"


def test_egress_probe_gets_no_credential(monkeypatch):
    monkeypatch.setenv(_PASSTHROUGH_ENV_VARS[0], "sk-secret-token-value")
    args, secret_env = _container_env_args(credentials=False)
    assert secret_env == {}
    assert not any(v in args for v in _PASSTHROUGH_ENV_VARS)
    assert "sk-secret-token-value" not in " ".join(args)


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
    # D27: --dangerously-skip-permissions (Task 13) makes permission_denials
    # unfalsifiable - it is always [] regardless of what the container
    # actually blocked. No assertion on it here or anywhere else.


# The tool-layer deny-list this test used to exercise (--disallowedTools
# "Bash(gh *),Bash(curl *),Bash(git fetch*)") was removed under Task 13/D22:
# it was defeated four ways in one review (absolute-path curl, python3
# urllib, git -C fetch, git ls-remote), and gh isn't even installed in the
# sealed image. The seal is now a network-layer property of the container,
# not a tool-layer property of the agent's choices - see
# tests/test_container.py::test_agent_cannot_reach_github_via_any_interpreter
# and ::test_container_blocks_github_allows_anthropic for the replacement
# coverage.
