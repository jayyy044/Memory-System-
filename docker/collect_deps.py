#!/usr/bin/env python3
"""D32: extract dependency *names* declared by a workspace WITHOUT ever
executing any of its code - `pip install <path>` (the previous approach)
runs PEP 517 build hooks (setup.py / pyproject.toml [build-system] code) as
whatever user calls it, and seal-egress.sh calls it as root, pre-seal, with
the network open. A planted build hook in the (agent-writable) workspace
would then run as root with full network access on the container's next
start - a complete, reusable defeat of the seal.

D36: validation is per-SPEC, applied to EVERY source. The first version of
this file validated only requirements.txt lines and appended
`[project.dependencies]` strings raw, so a workspace pyproject declaring
`dependencies = ["/workspace/evilpkg"]` put a path straight back into
`pip3 install` and restored the whole hole. Reproduced through the
production egress_sealed() path: the seal reported healthy while
evilpkg/setup.py ran as root pre-seal. There is no trusted source here -
pyproject.toml and requirements.txt are both files the agent can write.

Only tomllib (stdlib, pure parsing, no code execution) and line-regex
matching are used here. Prints one pip-installable specifier per line,
whitespace-free, so the caller can feed them to `pip install -r` with no
shell word-splitting involved at all.

Scope: this reads ONLY [project.dependencies] and requirements.txt - not
[build-system].requires, [project.optional-dependencies], setup.cfg,
Pipfile, or [tool.poetry.dependencies]. Those are not attack vectors (never
read, so nothing in them reaches pip), but a corpus repo that declares its
deps only in one of them will have them silently not installed - that looks
like a broken repo, not a harness limitation, unless you already know this.
"""
import re
import sys
from pathlib import Path

# Plain PEP 508 specifier only: name, optional [extras], optional version
# constraint. Anything else is rejected - a bare path, `-e`/`--editable`,
# `-r`/`--requirement` (a recursive include could point right back into the
# workspace), a `file:`/`https:` URL, a PEP 508 direct reference
# (`name @ /workspace/evil`), an environment marker (`; sys_platform==...`),
# or a pip requirements-file option line. Rejecting is deliberately the
# default: an unrecognized spec is skipped with a message, never passed
# through on the theory that it is probably fine.
_SPEC = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]*(\[[A-Za-z0-9,._-]+\])?"
    r"(\s*([=<>!~]=?|===)\s*[A-Za-z0-9.*+!_-]+(\s*,\s*([=<>!~]=?|===)\s*[A-Za-z0-9.*+!_-]+)*)?$"
)


def normalize(spec: object, source: str) -> str | None:
    """Returns the whitespace-free form of `spec` if it is a plain PEP 508
    requirement, else None (with a reason on stderr). Path-likeness is
    checked explicitly before the regex - the regex already rejects these,
    but a named check keeps the intent legible and the failure message
    useful."""
    if not isinstance(spec, str):
        print(f"collect_deps: skipping non-string {source} entry: {spec!r}", file=sys.stderr)
        return None
    if any(sep in spec for sep in ("/", "\\", "@", ":")) or spec.strip().startswith("."):
        print(f"collect_deps: skipping path-like/URL {source} entry: {spec!r}", file=sys.stderr)
        return None
    if not _SPEC.match(spec.strip()):
        print(f"collect_deps: skipping unsafe/unsupported {source} entry: {spec!r}", file=sys.stderr)
        return None
    # Only removes whitespace _SPEC already permitted (around comparison
    # operators); validation ran on the original, so this cannot turn a
    # rejected string into an accepted one.
    return re.sub(r"\s+", "", spec.strip())


def main(workspace: Path) -> int:
    raw: list[tuple[str, object]] = []

    pyproject = workspace / "pyproject.toml"
    if pyproject.exists():
        import tomllib
        try:
            data = tomllib.loads(pyproject.read_text())
        except Exception as e:
            print(f"collect_deps: pyproject.toml is not valid TOML: {e}", file=sys.stderr)
            return 1
        project = data.get("project")
        deps = project.get("dependencies", []) if isinstance(project, dict) else []
        if isinstance(deps, list):
            raw += [("pyproject.toml", d) for d in deps]

    req = workspace / "requirements.txt"
    if req.exists():
        for line in req.read_text().splitlines():
            line = line.split("#", 1)[0].strip()
            if line:
                raw.append(("requirements.txt", line))

    for source, spec in raw:
        norm = normalize(spec, source)
        if norm is not None:
            print(norm)
    return 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1] if len(sys.argv) > 1 else "/workspace")))
