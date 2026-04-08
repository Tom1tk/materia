import json
import logging
import memory as mem
import llm
import config

logger = logging.getLogger(__name__)

INTENT_SCHEMA = {
    "type": "object",
    "properties": {
        "mode": {"type": "string", "enum": ["chat", "simple_tool", "agentic_task"]},
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
    "required": ["mode", "action", "params", "reasoning", "needs_followup"],
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

## Mode selection — set `mode` to one of:

**chat** — pure conversation, factual questions, explanations. No tool needed.
  Examples: "what time is it", "explain cron syntax", "how are you"

**simple_tool** — a clear single-step action maps directly to one tool.
  Examples: "list my scripts", "what's the weather script schedule", "save my timezone as Europe/London",
  "run the network scanner", "search for python asyncio tutorial"

**agentic_task** — requires investigation, multiple steps, or verification before the task is complete.
  Examples: "why isn't weather-morning running", "check disk usage and clean up if needed",
  "fix the cron for weather-event", "make sure the HN briefing script actually works",
  "install netifaces and verify it imported correctly", "set up a daily briefing"
  Use this whenever the task involves: debugging, multi-step sequences, checking then acting,
  or any uncertainty about what needs to happen. Set `action` to "agentic_task".

## Routing rules for simple_tool — follow exactly:

**edit_script** — user wants to fix, edit, update, modify, or debug an existing script.
  Trigger words: fix, edit, update, modify, change, debug, broken, error, issue, wrong.
  Set `raw` to the script name or best guess. Set `description` to what needs fixing.

**run_shell** — user wants to run a command, install a package, delete/rename/move a file, or check system status.
  Set `raw` to the ACTUAL shell command (not the user's words).
  Examples: "install netifaces" → raw: "pip install netifaces"
            "check disk space" → raw: "df -h"
            "delete foo.py" → raw: "rm /opt/tgbot/scripts/foo.py"

**run_script** — user wants to execute an existing script. Set `raw` to script name.

**create_script** — creating a brand new script only.

**chat** — mode=chat, action=chat for genuine conversation when no tool applies.

Always return valid JSON with all required fields. For agentic_task, set action="agentic_task"."""

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
            "mode": "chat",
            "action": "chat",
            "params": {"raw": user_message},
            "reasoning": "fallback due to classification error",
            "needs_followup": False
        }
