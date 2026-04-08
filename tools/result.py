from dataclasses import dataclass, field
from typing import Literal


@dataclass
class ToolResult:
    status: Literal["ok", "error"]
    output: str           # raw text for LLM consumption
    display: str | None = None  # Markdown for the user (falls back to output)
    metadata: dict = field(default_factory=dict)  # exit_code, duration_ms, etc.

    @property
    def user_text(self) -> str:
        return self.display if self.display is not None else self.output

    @classmethod
    def ok(cls, output: str, display: str | None = None, **metadata) -> "ToolResult":
        return cls(status="ok", output=output, display=display, metadata=metadata)

    @classmethod
    def error(cls, output: str, display: str | None = None, **metadata) -> "ToolResult":
        return cls(status="error", output=output, display=display, metadata=metadata)
