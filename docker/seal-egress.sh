#!/bin/sh
set -eu
for ip in $(getent ahostsv4 "${ANTHROPIC_HOST:-api.anthropic.com}" | awk '{print $1}' | sort -u); do
  iptables -A OUTPUT -d "$ip" -j ACCEPT
done
iptables -A OUTPUT -o lo -j ACCEPT
# DNS: allow only the resolver(s) actually configured, not UDP:53 to any
# host - an unrestricted `-p udp --dport 53 -j ACCEPT` is a DNS-tunneling
# exfil channel (any attacker-controlled resolver on port 53 would pass).
for ns in $(awk '/^nameserver/{print $2}' /etc/resolv.conf); do
  iptables -A OUTPUT -d "$ns" -p udp --dport 53 -j ACCEPT
done
iptables -A OUTPUT -j DROP

# Bind-mounted host dirs (workspace, HOME, CLAUDE_CONFIG_DIR) show up owned
# root:root mode 700 through the VM's virtiofs layer regardless of host
# ownership - traversable only by root. uid 1000 needs in; nothing in these
# per-run temp dirs is secret from the agent it's about to become.
chmod 0777 /workspace "${HOME:-/root}" "${CLAUDE_CONFIG_DIR:-/root}" 2>/dev/null || true

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
# after a drop leaves CapBnd unchanged). But PR_CAPBSET_DROP itself requires
# CAP_SETPCAP in the caller's effective set, so the container must be started
# with --cap-add=NET_ADMIN --cap-add=SETPCAP --cap-drop=ALL (see driver.py) -
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
exec setpriv --reuid=1000 --regid=1000 --init-groups \
  --bounding-set=-net_admin,-setpcap,-setuid,-setgid \
  --inh-caps=-net_admin,-setpcap,-setuid,-setgid \
  --ambient-caps=-net_admin,-setpcap,-setuid,-setgid "$@"
