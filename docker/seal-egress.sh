#!/bin/sh
set -eu

# D28: install the arm's dependencies BEFORE any egress rule exists, while
# the network is still fully open - the image ships only pytest, and pip
# cannot reach PyPI once the seal below goes up. Generic across corpus
# repos: whichever of these the workspace declares. Fails loud - a broken
# install must not silently proceed into a container that then also can't
# reach PyPI to fix itself.
if [ -f /workspace/pyproject.toml ] || [ -f /workspace/setup.py ]; then
  pip3 install --no-cache-dir --break-system-packages /workspace \
    || { echo "seal-egress: dependency install from /workspace failed" >&2; exit 1; }
elif [ -f /workspace/requirements.txt ]; then
  pip3 install --no-cache-dir --break-system-packages -r /workspace/requirements.txt \
    || { echo "seal-egress: dependency install from requirements.txt failed" >&2; exit 1; }
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
# I3: the bind-mounted workspace/HOME/CONFIG dirs land root:root mode 700
# through the VM's virtiofs layer, unreachable by uid 1000. A `chmod 0777`
# on them would "fix" that by mutating the HOST directory's permission bits
# permanently (same inode, bind mount) - verified, and never reverted after
# the container exits. Keeping CAP_DAC_OVERRIDE (added to the container's
# starting caps, never dropped here) instead: it bypasses file/dir
# permission checks for this process without touching the mode bits at all.
# Verified: uid 1000 with only DAC_OVERRIDE can read/write/list a 700
# root-owned directory, and `stat` on that directory from outside the
# container still shows unchanged "700 root" throughout and after.
exec setpriv --reuid=1000 --regid=1000 --init-groups \
  --bounding-set=-net_admin,-setpcap,-setuid,-setgid \
  --inh-caps=-net_admin,-setpcap,-setuid,-setgid,+dac_override \
  --ambient-caps=-net_admin,-setpcap,-setuid,-setgid,+dac_override "$@"
