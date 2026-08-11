import json
import os
import re
import subprocess
import tempfile
import time
import uuid
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
# I3: DAC_OVERRIDE is kept (never dropped in seal-egress.sh's final setpriv
# call) so uid 1000 can read/write the bind-mounted workspace/HOME/CONFIG
# dirs, which land root:root mode 700 through the VM's virtiofs layer.
# The alternative (chmod the mount) mutates the HOST directory's real
# permission bits permanently - verified, and never reverted.
_CAP_ARGS = [
    "--cap-drop=ALL",
    "--cap-add=NET_ADMIN", "--cap-add=SETPCAP",
    "--cap-add=SETUID", "--cap-add=SETGID",
    "--cap-add=DAC_OVERRIDE",
]

# D26: WebSearch/WebFetch execute server-side on Anthropic's infrastructure
# and return content over the very connection the seal allowlists -
# iptables never sees a GitHub-bound packet for these. Unlike the failed
# Bash denylist (interpreters have alternate paths), a server-side tool has
# exactly one path, so denying it at the tool layer is airtight here.
_DISALLOWED_TOOLS = "WebSearch,WebFetch"


def _ensure_image() -> str:
    """Build (or reuse, via Docker's layer cache) the sealed execution
    image. A no-op rebuild after the first call costs a few hundred ms."""
    subprocess.run(
        ["docker", "build", "-q", "-t", _IMAGE_TAG, str(_DOCKER_DIR)],
        check=True, capture_output=True, text=True,
    )
    return _IMAGE_TAG


def _docker_run(args: list[str], *, timeout_s: int) -> subprocess.CompletedProcess:
    """I1: `docker run --rm` on a client-side subprocess timeout only kills
    the docker CLI, not the container - it keeps running detached,
    bind-mounted workspace and all, billing. Always pass --name so a
    timeout can clean up the actual container too.

    `docker kill` alone is not enough: verified live that a timeout hitting
    early (client killed before the container leaves "Created" for
    "Running") leaves it un-killable - `kill` signals a running process and
    is a no-op on one that never started. `docker rm -f` removes it in any
    state (created, running, or already exited), so that's what runs here."""
    name = f"membench-{uuid.uuid4().hex[:12]}"
    cmd = ["docker", "run", "--rm", "--name", name, *args]
    try:
        return subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout_s, stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        # Killing the client that early can race the daemon still finishing
        # the container's own create/start - verified live: an immediate
        # `docker rm -f` right after the kill can find nothing yet and
        # silently no-op, leaving the container to appear a moment later.
        # A few short retries cover that window; ponytail: fixed small
        # retry count, not a real backoff loop - raise the retry cap if
        # this is ever observed to still leak in practice.
        for _ in range(5):
            if subprocess.run(["docker", "rm", "-f", name], capture_output=True).returncode == 0:
                break
            time.sleep(0.5)
        raise


def tools_from_init(raw: str) -> list[str]:
    """Pulls the `tools` list out of the stream-json init event. Emitted by
    the CLI before any model turn, so this is checkable even when auth
    fails - and deterministic in a way the model's own prose never is."""
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            evt = json.loads(line)
        except json.JSONDecodeError:
            continue
        if evt.get("type") == "system" and evt.get("subtype") == "init":
            return evt.get("tools", [])
    return []


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
        proc = _docker_run(
            [
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
                # internet access", which this is. D26: WebSearch/WebFetch
                # are the one exception - they run server-side, outside the
                # container entirely, so they're still denied explicitly.
                "--dangerously-skip-permissions",
                "--disallowedTools", _DISALLOWED_TOOLS,
            ],
            timeout_s=timeout_s,
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
    reachable}`. A healthy seal is `{"anthropic": True, "github": False}`.

    Fails loud (D13/D17 precedent: unknown state is never reported as a
    pass) rather than inferring "reachable" from a missing marker - a
    failed `docker run` (bad image, daemon hiccup: rc=125, empty stdout)
    used to read as `{"anthropic": True, "github": True}` by the old
    absence-based check, indistinguishable from a real double-reach."""
    image = _ensure_image()
    proc = _docker_run(
        [
            *_CAP_ARGS,
            "-v", f"{workdir}:{_CONTAINER_WORKDIR}",
            image,
            "sh", "-c",
            # `; true` at the end: a blocked github curl exits nonzero, and
            # that becomes the CONTAINER's exit code (which docker run then
            # passes through as its own rc) - that's the expected healthy
            # case, not a docker-level failure, so it must not trip the
            # returncode check below. seal-egress.sh's own failure exits
            # (e.g. I2's empty-allowlist guard) happen before this command
            # even runs and still propagate correctly.
            "curl -s -m 5 -o /dev/null -w 'anthropic=%{http_code}\\n' https://api.anthropic.com/; "
            "curl -s -m 5 -o /dev/null -w 'github=%{http_code}\\n' https://api.github.com/; true",
        ],
        timeout_s=60,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"egress_sealed: `docker run`/seal-egress.sh failed (rc={proc.returncode}): {proc.stderr.strip()}"
        )
    out = proc.stdout
    anthropic_m = re.search(r"anthropic=(\d{3})", out)
    github_m = re.search(r"github=(\d{3})", out)
    if not anthropic_m or not github_m:
        raise RuntimeError(f"egress_sealed: probe markers missing from container output: {out!r}")
    return {
        "anthropic": anthropic_m.group(1) != "000",
        "github": github_m.group(1) != "000",
    }
