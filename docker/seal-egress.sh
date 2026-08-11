#!/bin/sh
set -eu

# D32/N1: never `pip install <path>` against the agent-writable workspace -
# that runs PEP 517 build hooks (setup.py / pyproject.toml [build-system]
# code) as whatever calls it, and this runs as root, pre-seal, network open.
# A build hook planted in /workspace (which the agent can freely write) would
# then run as root with full network access on the container's NEXT start
# (another run_agent, an egress_sealed(wd), a multi-episode arm) - a
# complete, reusable defeat of the seal. Verified live: the reviewer's planted
# hook wrote `uid=0 github_http=200` back into the workspace under the old
# `pip3 install /workspace` version of this script.
#
# collect_deps.py only parses declared metadata (tomllib + line-regex) and
# never executes anything from the workspace; the names it prints are
# installed from PyPI by name, never by path. Fails loud - a broken parse or
# install must not silently proceed.
# D36: the spec list goes through a file, not a shell variable. The previous
# `pip3 install $deps` was an UNQUOTED expansion of workspace-derived text -
# word-splitting and globbing both applied to it. Writing to a root-owned
# path outside the mount and using `pip -r` means no shell expansion of
# workspace content happens at all; collect_deps.py has already rejected
# every entry that is not a plain PEP 508 name/extras/version spec (from
# BOTH pyproject.toml and requirements.txt - validating only the latter is
# what left this hole open twice).
# S3/D54: at SCORING time, membench/runner.py sets MEMBENCH_FROZEN_DEPS to a
# file it already validated on the HOST (via this same collect_deps.py,
# reused) from task.base_sha - what was on disk before the agent's episode
# started - and mounts it read-only outside /workspace. When present, install
# THAT instead of re-deriving from /workspace: re-deriving at scoring time
# means the agent's own requirements.txt/pyproject.toml edits register
# whatever they want as root, pre-seal, in the SAME container that then
# scores the agent's own work - demonstrated, a workspace requirements.txt of
# `pytest-timeout==2.3.1` registered a plugin inside the scoring run, and
# `pytest==7.4.4` downgraded the image's pinned pytest outright. run_agent's
# own container never sets this var, so its behavior (derive from the live,
# agent-editable /workspace - the agent needs this to run its own pytest
# during the episode) is unchanged.
if [ -n "${MEMBENCH_FROZEN_DEPS:-}" ] && [ -f "$MEMBENCH_FROZEN_DEPS" ]; then
  deps_file="$MEMBENCH_FROZEN_DEPS"
else
  deps_file=/run/membench-deps.txt
  python3 /usr/local/bin/collect_deps.py /workspace > "$deps_file" \
    || { echo "seal-egress: dependency name extraction failed" >&2; exit 1; }
fi
# D41: --only-binary=:all: forbids pip from falling back to an sdist for
# ANY of these names. A validated spec is a NAME, not a promise of a wheel -
# an sdist install runs that package's setup.py, and this call is still
# root, pre-seal, network open. Without this flag, a workspace declaring the
# fully-valid `dependencies = ["sgmllib3k"]` (no wheel on PyPI) makes pip
# fetch the sdist and execute its setup.py as root before the seal is up -
# reviewer-demonstrated. This can fail an install that would have succeeded
# via sdist; that is the intended tradeoff, not a bug to work around.
if [ -s "$deps_file" ]; then
  pip3 install --no-cache-dir --break-system-packages --only-binary=:all: -r "$deps_file" \
    || { echo "seal-egress: dependency install failed" >&2; exit 1; }
fi

# D25/D29: resolve every A record while still root and network is up, pin
# each into /etc/hosts, THEN drop UDP:53 entirely (below) rather than
# allowlisting it - closes DNS as an egress channel completely instead of
# leaving the configured resolver reachable as a tunneling path. Verified:
# curl by hostname still reaches api.anthropic.com (real HTTP response)
# with UDP:53 fully dropped; `getent`/dig for any other name fails.
host="${ANTHROPIC_HOST:-api.anthropic.com}"
rule_count=0
for ip in $(getent ahostsv4 "$host" | awk '{print $1}' | sort -u); do
  echo "$ip $host" >> /etc/hosts
  iptables -A OUTPUT -d "$ip" -j ACCEPT
  rule_count=$((rule_count + 1))
done
# I2: fail-open guard - a resolution failure (typo'd host, DNS outage) must
# never produce a healthy-looking container with an empty allowlist. That's
# indistinguishable from a bad agent run without this check.
if [ "$rule_count" -eq 0 ]; then
  echo "seal-egress: resolved zero addresses for $host - refusing to start with an empty allowlist" >&2
  exit 1
fi
iptables -A OUTPUT -o lo -j ACCEPT
iptables -A OUTPUT -j DROP

# v6: no legitimate use (Anthropic access above is resolved/pinned as v4
# only) and no v6 route exists on this host to verify against either way -
# rather than leave that "unverified, assumed fine", drop all v6 egress
# unconditionally so it's closed by construction, not by absence of a route.
ip6tables -A OUTPUT -o lo -j ACCEPT
ip6tables -A OUTPUT -j DROP

# I3 / HOME+CONFIG: these are container-local now (not bind-mounted - see
# driver.py), so `mkdir`+`chown` here never touches anything host-visible.
# Only /workspace remains a bind mount that still needs DAC_OVERRIDE below.
# Defaulted rather than required (`${VAR:-default}`, safe under `set -u`):
# this script is the one place that contract has to hold, not every caller
# of the image - membench/runner.py (Task 4, built on _docker_run/_CAP_ARGS
# from this file) doesn't set either and broke under `set -u` the first time
# this was a hard requirement instead of a default.
HOME="${HOME:-/run/membench/home}"
CLAUDE_CONFIG_DIR="${CLAUDE_CONFIG_DIR:-/run/membench/config}"
export HOME CLAUDE_CONFIG_DIR
mkdir -p "$HOME" "$CLAUDE_CONFIG_DIR"
chown -R 1000:1000 "$HOME" "$CLAUDE_CONFIG_DIR"

# D23: --inh-caps/--ambient-caps alone do not stop a root process from
# regaining NET_ADMIN after exec (root gets capabilities from the bounding
# set, not from inherited/ambient, and a root process always has its full
# bounding set available at exec) - verified empirically, see
# task-13-report.md: after `--inh-caps=-net_admin --ambient-caps=-net_admin`
# alone, /proc/self/status still showed CAP_NET_ADMIN in CapBnd/CapEff and
# `iptables -F` still succeeded (exit 0) inside the sealed shell.
#
# --bounding-set actually removes it via prctl(PR_CAPBSET_DROP), which can
# only shrink and never regrow (confirmed: `setpriv --bounding-set=+net_admin`
# after a drop leaves CapBnd unchanged). PR_CAPBSET_DROP itself requires
# CAP_SETPCAP in the caller, so the container must be started with
# --cap-add=NET_ADMIN --cap-add=SETPCAP --cap-drop=ALL (see driver.py) -
# SETPCAP is then dropped from the bounding set in this same call so the
# agent never holds it either. --cap-drop=ALL also strips Docker's default
# NET_RAW, which would otherwise let the agent open an AF_PACKET socket and
# hand-craft link-layer frames that bypass the iptables OUTPUT chain
# entirely (verified: PermissionError without NET_RAW; socket.socket(AF_PACKET,
# SOCK_RAW) succeeds with it).
#
# Also drops root -> uid 1000 (the "node" user the base image already
# creates) in the same call: claude refuses --dangerously-skip-permissions
# while running as root/sudo, and this exec is the one place root privilege
# is still held, so it's dropped for good right here alongside the caps.
#
# I3: /workspace lands root:root mode 700 through the VM's virtiofs layer,
# unreachable by uid 1000. A `chmod 0777` on it would "fix" that by mutating
# the HOST directory's permission bits permanently (same inode, bind mount) -
# verified, and never reverted after the container exits. Keeping
# CAP_DAC_OVERRIDE (added to the container's starting caps, never dropped
# here) instead: it bypasses file/dir permission checks for this process
# without touching the mode bits at all. Verified: uid 1000 with only
# DAC_OVERRIDE can read/write/list a 700 root-owned directory, and `stat` on
# that directory from outside the container still shows unchanged "700 root"
# throughout and after. Reconsidered per review round 2: still needed -
# HOME/CONFIG no longer need it (moved off the bind-mount path above), but
# /workspace must stay bind-mounted (the harness reads results back from it
# on the host) and reconciling "agent must run as non-root" with "mount
# lands root-owned" has no narrower fix than this. Blast radius of the
# breadth this grants (uid 1000 can rewrite any root-owned file INSIDE the
# container, e.g. this script's own on-disk copy, /etc/hosts) is contained
# by --rm: nothing written to container-local paths (as opposed to the
# /workspace bind mount) survives past this one run.
exec setpriv --reuid=1000 --regid=1000 --init-groups \
  --bounding-set=-net_admin,-setpcap,-setuid,-setgid,-chown \
  --inh-caps=-net_admin,-setpcap,-setuid,-setgid,-chown,+dac_override \
  --ambient-caps=-net_admin,-setpcap,-setuid,-setgid,-chown,+dac_override "$@"
