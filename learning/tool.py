import json
import subprocess

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
        out = out[:LIMIT] + f"\n[truncated, {len(out)-LIMIT} more chars — narrow the command: head, tail, grep, wc -l]"

    return f"exit code: {p.returncode}\n{out}"

