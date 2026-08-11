import json
import os
import subprocess
import tempfile
from pathlib import Path

from membench.models import ToolCall, Transcript

# Denied at the tool layer. Verified live against claude 2.1.227 (see
# task-1-report.md): WebFetch/WebSearch disappear from the tool list entirely;
# "Bash(gh *)" / "Bash(curl *)" / "Bash(git fetch*)" block those specific
# commands (recorded in result.permission_denials) while leaving Bash itself
# usable for git status/log/diff and pytest. Bash cannot be denied wholesale -
# the arms must run pytest in the workspace.
_DISALLOWED_TOOLS = "WebFetch,WebSearch,Bash(gh *),Bash(curl *),Bash(git fetch*)"

# Forwarded from the operator's real environment when present; everything else
# the child sees comes from the explicit allowlist in _sealed_env. Required:
# setting CLAUDE_CONFIG_DIR at all (even to its own default path, verified live)
# disables the CLI's Keychain OAuth lookup, so a sealed run cannot authenticate
# without one of these.
_PASSTHROUGH_ENV_VARS = ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY")


def _sealed_env(config_dir: Path, home_dir: Path) -> dict:
    """Explicit child environment. Never inherit the parent's os.environ."""
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),  # to find git/python/claude
        "HOME": str(home_dir),  # fresh per-run dir: no host dotfiles/creds
        "CLAUDE_CONFIG_DIR": str(config_dir),  # fresh per-run dir: no host CLAUDE.md/hooks/plugins/skills/auto-memory
        "TZ": "UTC",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "GH_TOKEN": "",
        "GITHUB_TOKEN": "",
    }
    for var in _PASSTHROUGH_ENV_VARS:
        if os.environ.get(var):
            env[var] = os.environ[var]
    return env


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
    with tempfile.TemporaryDirectory(prefix="membench-cfg-") as config_dir, \
         tempfile.TemporaryDirectory(prefix="membench-home-") as home_dir:
        proc = subprocess.run(
            [
                "claude", "-p", prompt,
                "--output-format", "stream-json",
                "--verbose",
                "--max-turns", str(max_turns),
                "--model", model,
                "--permission-mode", "acceptEdits",
                "--disallowedTools", _DISALLOWED_TOOLS,
            ],
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            stdin=subprocess.DEVNULL,   # without this every run stalls 3s waiting on stdin
            env=_sealed_env(Path(config_dir), Path(home_dir)),
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
