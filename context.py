import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import config
import memory as mem

logger = logging.getLogger(__name__)
MEMORY_MD_PATH = "/opt/materia/MEMORY.md"

def count_tokens(text: str) -> int:
    return len(text) // 4

def count_messages_tokens(messages: list[dict]) -> int:
    return sum(count_tokens(m.get("content", "")) for m in messages)

@dataclass
class ContextUsage:
    system_tokens: int
    memory_tokens: int
    history_tokens: int
    message_tokens: int
    total: int
    limit: int

    @property
    def percent(self) -> float:
        return self.total / self.limit

    @property
    def status(self) -> str:
        if self.percent >= config.COMPACTION_THRESHOLD:
            return "critical"
        if self.percent >= config.WARN_THRESHOLD:
            return "warning"
        return "ok"

    def format_report(self) -> str:
        remaining = self.limit - self.total
        status_emoji = {"ok": "OK", "warning": "Warning", "critical": "Critical"}[self.status]
        return (
            f"Context usage: {self.total:,} / {self.limit:,} tokens ({self.percent*100:.0f}%)\n"
            f"├─ System + manifest:  {self.system_tokens} tokens\n"
            f"├─ Memory:             {self.memory_tokens} tokens\n"
            f"├─ Conversation:     {self.history_tokens} tokens\n"
            f"└─ Estimated margin: {remaining:,} tokens remaining\n\n"
            f"Status: {status_emoji}\n"
            f"Compaction triggers at {config.COMPACTION_THRESHOLD*100:.0f}% ({int(config.CONTEXT_LIMIT * config.COMPACTION_THRESHOLD):,} tokens)"
        )

async def check_and_compact(force: bool = False) -> bool:
    """Check if compaction is needed and run it if so. Returns True if compaction ran."""
    history = await mem.conversation_get_all()
    history_tokens = count_messages_tokens(history)
    if force or (history_tokens / config.CONTEXT_LIMIT >= config.COMPACTION_THRESHOLD):
        await compact(history)
        return True
    return False

async def compact(history: list[dict] = None):
    """Compact conversation history into a summary stored in memory."""
    import llm
    if history is None:
        history = await mem.conversation_get_all()
    if not history:
        return

    history_text = "\n".join(f"{m['role'].upper()}: {m['content']}" for m in history)
    messages = [
        {"role": "system", "content": (
            "You are a memory compaction assistant. Write in British English, metric units, 24h time, ISO dates.\n"
            "Only record facts explicitly stated by the user or confirmed in tool output. "
            "If you cannot cite a specific turn as the source, omit the bullet. "
            "Do not infer, interpolate, or add context not present in the transcript."
        )},
        {"role": "user", "content": (
            "Summarise the following conversation into a compact memory note.\n"
            "Extract: key facts the user stated, decisions confirmed, tasks that actually completed, "
            "user preferences expressed directly, scripts or tools created. "
            "Write as concise bullet points. Omit small talk and speculation. Max 200 words.\n\n"
            f"{history_text}"
        )}
    ]

    summary = await llm.llm_plain(messages, max_tokens=400, temperature=0.2)
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    section = f"\n## {timestamp}\n{summary}\n"

    # Append to MEMORY.md
    md_path = Path(MEMORY_MD_PATH)
    if not md_path.exists():
        md_path.write_text("# Bot Memory\n")
    with open(md_path, "a") as f:
        f.write(section)

    # Extract key facts into SQLite memory table
    # Prefix keys with compacted_ so they can be distinguished from user-set facts
    lines = [l.strip("- •").strip() for l in summary.splitlines() if l.strip().startswith(("-", "•"))]
    for i, line in enumerate(lines[:10]):
        if ":" in line:
            key, val = line.split(":", 1)
            await mem.memory_set("compacted_" + key.strip().lower().replace(" ", "_"), val.strip())

    # Clear history, keep last 2
    await mem.conversation_clear(keep_last=2)
    logger.info(f"[Materia] Context compacted. {len(history)} messages -> summary")
