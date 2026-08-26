#!/usr/bin/env python3
"""SessionStart hook: report what previous sessions in this repo did.

Claude Code pipes {"session_id": ..., "cwd": ...} to stdin and adds this
script's stdout to the model's context. Registered under `SessionStart` in
settings.json, where the timeout is seconds - which is why everything here
is deterministic parsing and never an agent call.

Run it by hand the same way Claude Code does:

    echo '{"session_id":"x","cwd":"'"$PWD"'"}' | python3 -m extractor.hook
"""

from __future__ import annotations

import json
import sys
from collections import Counter

from extractor.sessionExtractor import sessionCalls, sessionFiles

# Only "Exit code N" means the agent's approach failed. Measured across 8 real
# sessions: 9 of 16 is_error results never executed at all - blocked by a
# pre-flight guard ("Brace expansion", "Contains shell syntax that cannot be
# statically..."), refused by the user, or over a size limit. Treating those as
# dead ends would tell the next session not to use tools that work fine.
_RAN_AND_FAILED = "Exit code"

_MAX_DEAD_ENDS = 8
_MAX_FILES = 6


def summarise(repo: str, liveSession: str | None) -> str:
    files = sessionFiles(repo, liveSession=liveSession)
    if not files:
        return ""

    failures: Counter[str] = Counter()
    touched: Counter[str] = Counter()
    calls = 0

    for path in files:
        for call in sessionCalls(path):
            calls += 1
            if call.target and call.name in ("Read", "Edit", "Write"):
                touched[call.target] += 1
            if call.ok is False and call.result.startswith(_RAN_AND_FAILED):
                failures[f"{call.name}: {call.target}"] += 1

    lines = [
        f"## Previous sessions in this repo: {len(files)} ({calls} tool calls)",
        "",
    ]

    if failures:
        shown = failures.most_common(_MAX_DEAD_ENDS)
        hidden = len(failures) - len(shown)
        # Never truncate silently: a list of 8 with no note reads as "that is
        # all of them", which is the same defect as reporting a partial run
        # as a complete one.
        head = f"### Ran and failed ({len(failures)} distinct"
        lines.append(head + (f", {hidden} not shown)" if hidden else ")"))
        for what, count in shown:
            lines.append(f"- {what}" + (f"  [x{count}]" if count > 1 else ""))
        lines.append("")

    if touched:
        shown = touched.most_common(_MAX_FILES)
        hidden = len(touched) - len(shown)
        head = f"### Most-touched files ({len(touched)} distinct"
        lines.append(head + (f", {hidden} not shown)" if hidden else ")"))
        for path, count in shown:
            lines.append(f"- {path}  [{count} ops]")
        lines.append("")

    lines.append(
        "NOTE: mechanical extraction only. 'Ran and failed' is every command "
        "that exited non-zero, including ones retried and later fixed - it is "
        "not yet filtered to genuine dead ends."
    )
    return "\n".join(lines)


def main() -> int:
    # A hook that raises is a hook that breaks every session start. Nothing
    # here is important enough to do that, so failure is silence.
    try:
        payload = json.load(sys.stdin)
        text = summarise(payload["cwd"], payload.get("session_id"))
    except Exception:
        return 0
    if text:
        sys.stdout.write(text + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
