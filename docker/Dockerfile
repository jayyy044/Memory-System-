# Network-sealed execution image for membench arms (Task 13).
# Node base already has git/python not preinstalled; add what run_agent needs:
# python3+pip+pytest for the arms, git for the workspace, iptables+setpriv for
# the egress seal, ca-certificates for TLS to api.anthropic.com.
FROM node:22-bookworm-slim@sha256:d649c27dae7ba0137b3cef5dd75baa422c08dc3d9e3fc0c23dfb172dc3cc6436

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-pip python3-venv git iptables util-linux \
        ca-certificates dnsutils curl \
    && rm -rf /var/lib/apt/lists/* \
    && pip3 install --no-cache-dir --break-system-packages pytest pytest-json-report \
    && npm install -g @anthropic-ai/claude-code@2.1.227 \
    && npm cache clean --force
# claude refuses --dangerously-skip-permissions as root/sudo ("cannot be
# used with root/sudo privileges for security reasons") - verified live
# against 2.1.227. seal-egress.sh needs root (NET_ADMIN/SETPCAP) to write
# the iptables rules and then drop capabilities, so it must also drop to
# an unprivileged user in the same setpriv call before exec'ing claude.
# Reuses uid 1000, which the node:22 base image already creates as "node" -
# no need to add a second one.

COPY seal-egress.sh /usr/local/bin/seal-egress.sh
RUN chmod +x /usr/local/bin/seal-egress.sh

WORKDIR /workspace
ENTRYPOINT ["/usr/local/bin/seal-egress.sh"]
