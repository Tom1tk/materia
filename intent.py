import json
import logging
import memory as mem
import llm
import config

logger = logging.getLogger(__name__)

INTENT_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string"},
        "params": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "description": {"type": "string"},
                "schedule": {"type": "string"},
                "test_first": {"type": "boolean"},
                "length": {"type": "string"},
                "topic": {"type": "string"},
                "raw": {"type": "string"}
            },
            "additionalProperties": False
        },
        "reasoning": {"type": "string"},
        "needs_followup": {"type": "boolean"}
    },
    "required": ["action", "params", "reasoning", "needs_followup"],
    "additionalProperties": False
}

async def get_manifest_text() -> str:
    try:
        with open("/opt/tgbot/manifest.json") as f:
            data = json.load(f)
        lines = []
        for t in data["tools"]:
            lines.append(f"- {t['name']}: {t['description']}")
        return "\n".join(lines)
    except Exception as e:
        logger.error(f"Failed to read manifest: {e}")
        return ""

async def classify_intent(user_message: str) -> dict:
    """Classify user message into a structured intent."""
    manifest_text = await get_manifest_text()
    memory_data = await mem.memory_get_all()

    memory_text = ""
    if memory_data:
        items = list(memory_data.items())[:20]
        memory_text = "\nUser preferences and context:\n" + "\n".join(f"- {k}: {v}" for k, v in items)

    # Last 2 messages for context
    recent = await mem.conversation_get(limit=2)
    recent_text = ""
    if recent:
        recent_text = "\nRecent conversation:\n" + "\n".join(f"{m['role'].upper()}: {m['content']}" for m in recent)

    system_prompt = f"""You are Materia — a personal assistant running locally on a Proxmox server.
You classify user messages into structured actions. Respond ONLY with valid JSON matching the schema.
Write in British English, metric units, 24h time, ISO dates.

Available tools:
{manifest_text}
{memory_text}
{recent_text}

Classify the user's message. Choose the most appropriate action from the tool list.
For casual conversation or questions, use "chat".
Always return valid JSON with all required fields."""

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_message}
    ]

    try:
        result = await llm.llm_structured(messages, INTENT_SCHEMA)
        logger.info(f"[Materia] Intent: {result['action']} — {result['reasoning']}")
        return result
    except Exception as e:
        logger.error(f"Intent classification failed: {e}")
        return {
            "action": "chat",
            "params": {"raw": user_message},
            "reasoning": "fallback due to classification error",
            "needs_followup": False
        }
