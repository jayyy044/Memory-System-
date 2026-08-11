import json
import os
import re
import subprocess
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

_REPO_ROOT = Path(__file__).resolve().parent.parent
_ENV_FILE = _REPO_ROOT / ".env"


def _dotenv_value(key: str) -> str | None:
    """Falls back to the repo-root .env when a credential isn't already in
    os.environ - subagents/pytest runs don't inherit the operator's shell,
    so the file is the only thing every process can see. Never logs the
    value. Minimal KEY=VALUE line parser (# comments, blank lines skipped) -
    no reason to add python-dotenv for one file read."""
    if not _ENV_FILE.exists():
        return None
    for line in _ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        if k.strip() == key:
            return v.strip().strip('"').strip("'")
    return None


def _credential(var: str) -> str | None:
    return os.environ.get(var) or _dotenv_value(var)

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
# call) so uid 1000 can read/write the bind-mounted /workspace dir, which
# lands root:root mode 700 through the VM's virtiofs layer. The alternative
# (chmod the mount) mutates the HOST directory's real permission bits
# permanently - verified, and never reverted. Reconsidered per review round
# 2: HOME/CONFIG no longer need this (they're container-local paths now,
# chown'd by seal-egress.sh itself - see below), only /workspace does, since
# that's the one mount the harness actually reads results back from.
# CHOWN: root needs it too (--cap-drop=ALL strips it from root same as
# everything else) to `chown` the container-local HOME/CONFIG dirs to uid
# 1000 before the privilege drop; dropped again in the same setpriv call,
# same as the rest - the agent never holds it.
_CAP_ARGS = [
    "--cap-drop=ALL",
    "--cap-add=NET_ADMIN", "--cap-add=SETPCAP",
    "--cap-add=SETUID", "--cap-add=SETGID",
    "--cap-add=DAC_OVERRIDE", "--cap-add=CHOWN",
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


def _scrub(text, secrets: list[str]):
    """D38: a credential must never survive into an exception, a traceback,
    or a CI log. Handles bytes as well as str: TimeoutExpired.output/.stderr
    are BYTES even when subprocess.run was called with text=True (the
    partial buffers are never decoded on the timeout path) - verified, this
    raised TypeError the first time through."""
    if not text:
        return text
    as_bytes = isinstance(text, bytes)
    marker = b"***REDACTED***" if as_bytes else "***REDACTED***"
    for s in secrets:
        if s:
            text = text.replace(s.encode() if as_bytes else s, marker)
    return text


def _docker_run(
    args: list[str], *, timeout_s: int, secret_env: dict[str, str] | None = None
) -> subprocess.CompletedProcess:
    """I1: `docker run --rm` on a client-side subprocess timeout only kills
    the docker CLI, not the container - it keeps running detached,
    bind-mounted workspace and all, billing. Always pass --name so a
    timeout can clean up the actual container too.

    `docker kill` alone is not enough: verified live that a timeout hitting
    early (client killed before the container leaves "Created" for
    "Running") leaves it un-killable - `kill` signals a running process and
    is a no-op on one that never started. `docker rm -f` removes it in any
    state (created, running, or already exited) - EXCEPT its own return code
    cannot be trusted to tell you which happened (N3): `docker rm -f` on a
    name that does not exist yet still exits 0, with "No such container"
    only on stderr - checking rc alone made the original retry loop break on
    iteration 0 unconditionally (verified: it "succeeded" whether or not
    anything was actually removed, which is exactly why 4/5 early-timeout
    runs still leaked under that version). Poll actual existence via
    `docker inspect` instead of trusting `rm`'s exit status.

    D38: `secret_env` values reach the container through THIS process's own
    environment - docker's `-e NAME` form (no `=`) tells the client to read
    the value from its own env - never as `-e NAME=VALUE` on argv, which is
    world-readable in host `ps` and, verified, ends up inside
    subprocess.TimeoutExpired (`token in repr(e.cmd)` was True) which this
    function re-raises. The re-raise is scrubbed as well, so a future caller
    that puts a secret back on argv still cannot leak it through default
    traceback rendering (format_exception respects __suppress_context__).
    That guarantee does not extend to code that walks __context__ directly -
    a structured logger or Sentry-style capture would still see the
    unscrubbed original in e.__context__, since `raise ... from None` only
    hides it from the default renderer, not from the object graph. No
    caller does this today."""
    name = f"membench-{uuid.uuid4().hex[:12]}"
    cmd = ["docker", "run", "--rm", "--name", name, *args]
    try:
        return subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout_s, stdin=subprocess.DEVNULL,
            env={**os.environ, **secret_env} if secret_env else None,
        )
    except subprocess.TimeoutExpired as e:
        # The daemon can still be finishing the container's own create/start
        # after the client is killed - a container that doesn't exist YET at
        # the first check can still appear a moment later. Loop rm+re-check
        # across a window rather than trusting a single pass either way.
        # ponytail: fixed retry count/interval, not a real backoff - raise
        # the cap if this is ever observed to still leak in practice.
        for _ in range(10):
            subprocess.run(["docker", "rm", "-f", name], capture_output=True)
            time.sleep(0.5)
            still_exists = subprocess.run(
                ["docker", "inspect", name], capture_output=True
            ).returncode == 0
            if not still_exists:
                break
        secrets = list((secret_env or {}).values()) + [
            v for v in (_credential(k) for k in _PASSTHROUGH_ENV_VARS) if v
        ]
        # `from None`: chaining would attach the ORIGINAL, unscrubbed
        # exception to the traceback and undo the whole point of this.
        raise subprocess.TimeoutExpired(
            cmd=[_scrub(a, secrets) for a in cmd],
            timeout=e.timeout,
            output=_scrub(e.output, secrets),
            stderr=_scrub(e.stderr, secrets),
        ) from None


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


def _container_env_args(*, credentials: bool) -> tuple[list[str], dict[str, str]]:
    """`-e` args for `docker run`, plus the secret values to inject via the
    client's own environment. Never forwards the host's os.environ - only
    this explicit allowlist reaches the container. HOME and
    CLAUDE_CONFIG_DIR point at container-local paths (never bind-mounted -
    review round 2: no reason to route them through the host filesystem at
    all when nothing needs to read them back afterward); seal-egress.sh
    creates and chowns them itself before dropping to uid 1000.

    D37: `credentials` is opt-in per call site, not a property of the
    image. Only run_agent needs a token; egress_sealed is a pure
    reachability probe. Passing one to it meant the pre-seal, root, network-
    open dependency-install stage could read the operator's real credential
    out of its own environment (reproduced: `token_visible=True
    token_len=108`). Nothing that runs before the seal is up ever sees one
    now - the credential-install stage and the probe both run with
    credentials=False.

    D38: credentials are emitted as bare `-e NAME` (docker reads the value
    from the client's environment) - never `-e NAME=VALUE`, which puts the
    token in host `ps` and in every exception carrying the argv."""
    env = {
        "HOME": _CONTAINER_HOME,  # fresh per-run, container-local: no host dotfiles/creds
        "CLAUDE_CONFIG_DIR": _CONTAINER_CONFIG,  # fresh per-run, container-local: no host CLAUDE.md/hooks/plugins/skills/auto-memory
        "TZ": "UTC",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }
    args = []
    for k, v in env.items():
        args += ["-e", f"{k}={v}"]
    secret_env: dict[str, str] = {}
    if credentials:
        for var in _PASSTHROUGH_ENV_VARS:
            val = _credential(var)
            if val:
                args += ["-e", var]  # name only; value travels via secret_env
                secret_env[var] = val
    return args, secret_env


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
    env_args, secret_env = _container_env_args(credentials=True)
    proc = _docker_run(
        [
            *_CAP_ARGS,
            "-v", f"{workdir}:{_CONTAINER_WORKDIR}",
            *env_args,
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
        secret_env=secret_env,
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
            # D37: credentials=False. This is a reachability probe - it runs
            # no agent and needs no token, and the dependency-install stage
            # inside the entrypoint runs as root, pre-seal, with the network
            # open, so anything in this container's environment is readable
            # by workspace-derived code at exactly the worst moment.
            *_container_env_args(credentials=False)[0],  # seal-egress.sh mkdir/chowns $HOME/$CLAUDE_CONFIG_DIR
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
