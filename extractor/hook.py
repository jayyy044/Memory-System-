"""SessionStart hook: what previous sessions in this repo already worked out.

Claude Code pipes {"session_id": ..., "cwd": ...} to stdin and adds this
script's stdout to the model's context, before the model sees anything.
Registered in .claude/settings.local.json.

Run it the way Claude Code does:

    echo '{"session_id":"x","cwd":"'"$PWD"'"}' | .venv/bin/python -m extractor.hook
"""

import json
import sys
from extractor.sessionExtractor import projectFiles

#Claude code has the cwd and the session id and will provide it in as stdin when calling this script
def main() -> int:
    payload = json.load(sys.stdin)
    print("cwd        :", payload["cwd"])
    print("session id :", payload["session_id"])
    print("transcript path:", payload["transcript_path"])
    transcriptPaths = projectFiles(payload["transcript_path"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
