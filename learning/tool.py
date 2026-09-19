import json
import subprocess
from pathlib import Path


bash_tool = {
    "type": "function",
    "function": {
        "name": "bash",
        "description": (
            "Run a shell command and return its output. "
            "Each call runs in a fresh shell, so cwd, env vars and background "
            "jobs do NOT persist between calls; use absolute paths or chain "
            "steps in one command. "
            "Use for file ops, scripts, git, and system inspection. "
            "Long-running processes should be backgrounded with '&', not run inline. "
            "Result includes exit code, check it before assuming success."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "A single command or pipeline. Chain steps with && so failures short-circuit."
                },
                "timeout": {
                    "type": "integer",
                    "description": "Max seconds before the command is killed. Default 30."
                }
            },
            "required": ["command"]
        }
    }
}
 
def bash(command: str, timeout: int = 30, cwd: str | None = None) -> str:
    timeout = min(timeout, 120)  # model ignores the schema default and asks for 10000s
    try:
        p = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
        )
        
    except subprocess.TimeoutExpired:
        return f"[timed out after {timeout}s]"

    out = (p.stdout + p.stderr).strip() or "(no output)"
    LIMIT = 2000
    if len(out) > LIMIT:
        half = LIMIT // 2
        out = out[:half] + f"\n[... {len(out) - LIMIT} chars omitted ...]\n" + out[-half:]

    return f"exit code: {p.returncode}\n{out}"


read_file_tool = {
    "type": "function",
    "function": {
        "name": "read_file",
        "description": (
            "Read a text file and return its contents with line numbers. "
            "Prefer this over 'cat' for reading files: output is numbered and "
            "windowed, so large files return a page instead of being truncated "
            "mid-stream. Reads at most 200 lines per call; if the file is longer "
            "the result says so and you can call again with a new offset. "
            "Use bash with grep to locate a line first, then read around it."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the file, relative to the working directory."
                },
                "offset": {
                    "type": "integer",
                    "description": "1-based line number to start from. Default 1."
                },
                "limit": {
                    "type": "integer",
                    "description": "How many lines to return. Default 200, max 200."
                }
            },
            "required": ["path"]
        }
    }
}
def read_file(path: str, offset: int = 1, limit: int = 200, cwd: str | None = None) -> str:
    limit = max(1, min(limit, 200))
    offset = max(1, offset)

    p = Path(cwd or ".") / path
    try:
        lines = p.read_text(errors="replace").splitlines()
    except FileNotFoundError:
        return f"error: no such file: {path}"
    except IsADirectoryError:
        return f"error: {path} is a directory"
    except UnicodeDecodeError:
        return f"error: {path} is not a text file"

    total = len(lines)
    if offset > total:
        return f"error: offset {offset} is past end of file ({total} lines)"

    window = lines[offset - 1: offset - 1 + limit]
    # cap total chars too: 200 lines x 400 chars is 80k, bash is capped at 2k
    CHARS = 2000
    used = 0
    for i, ln in enumerate(window):
        used += len(ln[:400]) + 8          # 8 for the number gutter
        if used > CHARS:
            window = window[:max(1, i)]
            break
    body = "\n".join(f"{offset + i:6d}  {ln[:400]}" for i, ln in enumerate(window))

    end = offset + len(window) - 1
    footer = ""
    if end < total:
        footer = f"\n\n[showing {offset}-{end} of {total} lines; call again with offset={end + 1}]"
    return body + footer

