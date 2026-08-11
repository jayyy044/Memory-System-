#!/usr/bin/env python3
"""D32: extract dependency *names* declared by a workspace WITHOUT ever
executing any of its code - `pip install <path>` (the previous approach)
runs PEP 517 build hooks (setup.py / pyproject.toml [build-system] code) as
whatever user calls it, and seal-egress.sh calls it as root, pre-seal, with
the network open. A planted build hook in the (agent-writable) workspace
would then run as root with full network access on the container's next
start - a complete, reusable defeat of the seal.

Only tomllib (stdlib, pure parsing, no code execution) and line-regex
matching are used here. Prints one pip-installable specifier per line.
"""
import re
import sys
from pathlib import Path

workspace = Path(sys.argv[1] if len(sys.argv) > 1 else "/workspace")
specs: list[str] = []

pyproject = workspace / "pyproject.toml"
if pyproject.exists():
    import tomllib
    try:
        data = tomllib.loads(pyproject.read_text())
    except Exception as e:
        print(f"collect_deps: pyproject.toml is not valid TOML: {e}", file=sys.stderr)
        sys.exit(1)
    specs += data.get("project", {}).get("dependencies", [])

# Plain PEP 508 specifier only: name, optional [extras], optional version
# constraint. Reject anything that could reach local code instead of PyPI -
# -e/--editable, file:, a bare path, or -r/--requirement (a recursive
# include could point right back into the workspace).
_SPEC = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]*(\[[A-Za-z0-9,._-]+\])?"
    r"(\s*([=<>!~]=?|===)\s*[A-Za-z0-9.*+!_-]+(\s*,\s*([=<>!~]=?|===)\s*[A-Za-z0-9.*+!_-]+)*)?$"
)
req = workspace / "requirements.txt"
if req.exists():
    for line in req.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        if not _SPEC.match(line):
            print(f"collect_deps: skipping unsafe/unsupported requirements.txt line: {line!r}", file=sys.stderr)
            continue
        specs.append(line)

for s in specs:
    print(s)
