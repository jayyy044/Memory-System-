#!/usr/bin/env bash
# SessionStart hook: show what Claude Code hands a hook on stdin.
#
# SessionStart stdout is added to the model's context, so whatever this prints
# becomes something Claude can see. Registered in .claude/settings.local.json.
set -euo pipefail

payload=$(cat)

echo "=== SessionStart hook fired ==="
echo "session_id : $(printf '%s' "$payload" | jq -r '.session_id // "(absent)"')"
echo "cwd        : $(printf '%s' "$payload" | jq -r '.cwd // "(absent)"')"
echo "source     : $(printf '%s' "$payload" | jq -r '.source // "(absent)"')"
echo "==============================="
