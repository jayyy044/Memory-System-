from dataclasses import dataclass, field


@dataclass
class ToolCall:
    name: str
    command: str | None = None
    file_path: str | None = None


@dataclass
class Transcript:
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    exit_code: int = 0
    raw: str = ""
    cost_usd: float = 0.0
    num_turns: int = 0
    stop_reason: str | None = None
    permission_denials: list = field(default_factory=list)
