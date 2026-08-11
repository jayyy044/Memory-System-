import json
import os
import subprocess
import tempfile
from pathlib import Path

from membench.models import ToolCall, Transcript

# Task 13 / D22: the container is the security boundary now, not the tool
# layer. --disallowedTools and --permission-mode acceptEdits are gone on
# purpose - the previous deny-list was defeated four ways in one review
# (absolute-path curl, python3 urllib, git -C fetch, git ls-remote) and the
# only thing that had actually been blocking Bash was acceptEdits rejecting
# nearly everything, pytest included. Full Bash, no denylist; egress is shut
# at the network layer by docker/seal-egress.sh instead.
_IMAGE_TAG = "membench-agent:latest"
_DOCKER_DIR = Path(__file__).resolve().parent.parent / "docker"
_CONTAINER_WORKDIR = "/workspace"
_CONTAINER_HOME = "/run/membench/home"
_CONTAINER_CONFIG = "/run/membench/config"

# Forwarded into the container when present; everything else the child sees
# comes from the explicit -e list in _container_env_args. Required: setting
# CLAUDE_CONFIG_DIR at all (even to its own default path, verified live)
# disables the CLI's Keychain OAuth lookup, so a sealed run cannot
# authenticate without one of these (and Keychain isn't reachable from a
# Linux container anyway).
_PASSTHROUGH_ENV_VARS = ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY")

# D23: container must start with exactly these capabilities, all of them
# spent by seal-egress.sh before the agent gets control. NET_ADMIN writes
# the egress iptables rules; SETUID/SETGID drop root -> uid 1000 (claude
# refuses --dangerously-skip-permissions as root - verified live against
# 2.1.227); SETPCAP is needed to then drop all four (itself included) from
# the bounding set via prctl(PR_CAPBSET_DROP), which requires CAP_SETPCAP
# in the caller. --cap-drop=ALL also strips Docker's default NET_RAW, which
# would otherwise let the agent open an AF_PACKET socket and hand-craft
# link-layer frames that bypass the iptables OUTPUT chain entirely
# (verified empirically, see task-13-report.md).
_CAP_ARGS = [
    "--cap-drop=ALL",
    "--cap-add=NET_ADMIN", "--cap-add=SETPCAP",
    "--cap-add=SETUID", "--cap-add=SETGID",
]


def _ensure_image() -> str:
    """Build (or reuse, via Docker's layer cache) the sealed execution
    image. A no-op rebuild after the first call costs a few hundred ms."""
    subprocess.run(
        ["docker", "build", "-q", "-t", _IMAGE_TAG, str(_DOCKER_DIR)],
        check=True, capture_output=True, text=True,
    )
    return _IMAGE_TAG


def _container_env_args(home_dir: str = _CONTAINER_HOME, config_dir: str = _CONTAINER_CONFIG) -> list[str]:
    """`-e KEY=VALUE` args for `docker run`. Never forwards the host's
    os.environ - only this explicit allowlist reaches the container."""
    env = {
        "HOME": home_dir,  # fresh per-run dir: no host dotfiles/creds
        "CLAUDE_CONFIG_DIR": config_dir,  # fresh per-run dir: no host CLAUDE.md/hooks/plugins/skills/auto-memory
        "TZ": "UTC",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }
    for var in _PASSTHROUGH_ENV_VARS:
        if os.environ.get(var):
            env[var] = os.environ[var]
    args = []
    for k, v in env.items():
        args += ["-e", f"{k}={v}"]
    return args


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
    """Unchanged signature/return (D24; Tasks 6-12 are written against
    both). Execution substrate is now `docker run` instead of a bare
    `claude` subprocess - stdout is still stream-json, so _parse_stream
    needs no changes."""
    image = _ensure_image()
    with tempfile.TemporaryDirectory(prefix="membench-cfg-") as config_dir, \
         tempfile.TemporaryDirectory(prefix="membench-home-") as home_dir:
        proc = subprocess.run(
            [
                "docker", "run", "--rm",
                *_CAP_ARGS,
                "-v", f"{workdir}:{_CONTAINER_WORKDIR}",
                "-v", f"{config_dir}:{_CONTAINER_CONFIG}",
                "-v", f"{home_dir}:{_CONTAINER_HOME}",
                *_container_env_args(),
                image,
                "claude", "-p", prompt,
                "--output-format", "stream-json",
                "--verbose",
                "--max-turns", str(max_turns),
                "--model", model,
                # D22: the container is the boundary, not the tool layer -
                # full Bash, no denylist, no acceptEdits gate. Recommended
                # by `claude --help` specifically "for sandboxes with no
                # internet access", which this is.
                "--dangerously-skip-permissions",
            ],
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


def egress_sealed(workdir: Path) -> dict:
    """Starts the sealed container and probes both hosts directly (no
    agent/credential involved) - `{"anthropic": reachable, "github":
    reachable}`. A healthy seal is `{"anthropic": True, "github": False}`."""
    image = _ensure_image()
    proc = subprocess.run(
        [
            "docker", "run", "--rm",
            *_CAP_ARGS,
            "-v", f"{workdir}:{_CONTAINER_WORKDIR}",
            image,
            "sh", "-c",
            "curl -s -m 5 -o /dev/null -w 'anthropic=%{http_code}\\n' https://api.anthropic.com/; "
            "curl -s -m 5 -o /dev/null -w 'github=%{http_code}\\n' https://api.github.com/",
        ],
        capture_output=True, text=True, timeout=60,
    )
    out = proc.stdout
    return {
        "anthropic": "anthropic=000" not in out,
        "github": "github=000" not in out,
    }
