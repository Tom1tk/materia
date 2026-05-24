from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

ToolHandler = Callable[[dict], Awaitable[str]]
ConfirmCheck = Callable[[dict], Optional[str]]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    handler: ToolHandler
    params: dict = field(default_factory=dict)
    added: str = ""

    # Markdown block injected into intent.py's routing rules section.
    # None = callable via slash but not advertised in LLM routing hints.
    intent_hint: Optional[str] = None

    # Returns an HTML warning string to require Yes/No confirmation, or None.
    confirm: Optional[ConfirmCheck] = None

    # Render output with Telegram Markdown v1 parse_mode.
    markdown: bool = False

    # Bot injects a `notify` async callback into params for progress updates.
    streams_progress: bool = False

    # Refresh the Telegram command menu after a successful run.
    refresh_commands_after: bool = False
