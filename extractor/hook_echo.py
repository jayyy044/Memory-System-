#!/usr/bin/env python3
"""Minimal SessionStart hook: print what Claude Code handed us, nothing else.

Exists to show the interface. Claude Code pipes one JSON object to stdin;
this prints every key it received.
"""

import json
import sys

payload = json.load(sys.stdin)

print("=== hook received on stdin ===")
for key in sorted(payload):
    print(f"  {key:18} = {payload[key]!r}")
print("==============================")
