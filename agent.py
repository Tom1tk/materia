"""
Materia agentic loop — ReAct-style multi-step execution.

Each iteration the LLM chooses a tool to call or declares it is finished.
The loop feeds real tool output back as observations so the model never
needs to fabricate results.
"""
import json
import logging
import time
from datetime import datetime
from typing import Callable, Awaitable
from zoneinfo import ZoneInfo

import config
import llm
import memory as mem
from tools.result import ToolResult

logger = logging.getLogger(__name__)

MAX_STEPS = getattr(config, "AGENT_MAX_STEPS", 6)
MAX_SECONDS = getattr(config, "AGENT_MAX_SECONDS", 600)

# JSON schema for each agent step
STEP_SCHEMA = {
    "type": "object",
    "properties": {
        "thought": {"type": "string"},
        "action": {"type": "string", "enum": ["tool_call", "finish"]},
        "tool": {"type": "string"},
        "params": {
            "type": "object",
            "properties": {
                "raw": {"type": "string"},
                "query": {"type": "string"},
                "description": {"type": "string"},
                "schedule": {"type": "string"},
                "test_first": {"type": "boolean"},
                "length": {"type": "string"},
                "topic": {"type": "string"}
            },
            "additionalProperties": False
        },
        "summary": {"type": "string"}
    },
    "required": ["thought", "action", "tool", "params", "summary"],
    "additionalProperties": False
}


def _build_agent_system_prompt(manifest_text: str, scripts_list: str) -> str:
    now = datetime.now(ZoneInfo(config.TIMEZONE))
    current_dt = now.strftime("%A, %Y-%m-%d %H:%M %Z")
    return f"""You are Materia — a local-first personal assistant running on a Debian 13 LXC on a Proxmox host.
You are operating in agentic mode: you will be called in a loop, calling one tool per turn until the task is done.
Write in British English, metric units, 24h time, ISO dates.

## System facts (authoritative — do not contradict these)
- Active model: {config.LLM_MODEL}
- Current date/time: {current_dt}
- Scripts on disk: {scripts_list}

## Available tools
{manifest_text}

## Creating tools — two paths

**Quick tool (hot-reload, no restart):** Use `create_tool`. The LLM generates a bare async function,
appends it to `tools/user_tools.py`, and registers it in `manifest.json` immediately.
Use this for simple, one-off tools where the user just wants something that works now.

**Plugin tool (drop-in, restart required):** Write a file to `tools/<name>.py` using `run_shell`
(or `create_script` if the file is complex). The file must follow this exact structure:

```python
from tools.spec import ToolSpec
from tools.registry import register

async def <name>(params: dict) -> str:
    # implementation
    return "result"

register(ToolSpec(
    name="<name>",
    description="One-line description shown in /tools and /help",
    handler=<name>,
    params={{"raw": "string — description of the arg"}},  # optional
    intent_hint="**<name>** — trigger words and routing instructions for the LLM",  # optional
    confirm=None,     # or: lambda params: "Warning HTML" | None
    markdown=False,   # True if output uses Markdown formatting
))
```

Use the plugin path when the tool needs: intent routing hints, confirmation prompts,
Markdown output, or when it should persist cleanly as a standalone file.
After writing the file, tell the user to run `sudo systemctl restart materia`.

## Agent rules (mandatory)
- You have REAL tool access. Use tools to verify before claiming results.
- Never fabricate tool output. If a step fails, report the actual error from the observation.
- Do not invent file contents, command results, or script behaviour you have not been shown.
- Call one tool per turn. Observe the result before deciding what to do next.
- When the task is complete (or you have enough information to give a final answer), set action="finish"
  and write a clear, honest summary of what was done and what the result was.
- If a tool repeatedly fails and you cannot make progress, set action="finish" and explain the failure.
- When debugging a script issue, always check `script_history` first to see recent run logs.
- When a script edit broke something and the user wants to undo, use `rollback_script`.
- You have a budget of {MAX_STEPS} steps. Be efficient.

## Output format
Always respond with valid JSON matching the schema.
- thought: one sentence explaining your reasoning for this step
- action: "tool_call" or "finish"
- tool: tool name (required when action=tool_call; empty string when action=finish)
- params: tool parameters (empty object when action=finish)
- summary: final answer for the user (required when action=finish; empty string when action=tool_call)"""


async def _execute_tool(tool_name: str, params: dict) -> ToolResult:
    """Invoke a tool from the TOOL_MAP and return a ToolResult."""
    import sys
    import traceback as tb_mod
    from router import TOOL_MAP
    from tools import registry

    handler = TOOL_MAP.get(tool_name)

    if handler is None:
        user_tools = sys.modules.get("tools.user_tools")
        if user_tools:
            handler = getattr(user_tools, tool_name, None)

    if handler is None:
        spec = registry.get(tool_name)
        if spec:
            handler = spec.handler

    if handler is None:
        return ToolResult.error(f"Unknown tool: {tool_name!r}")

    t0 = time.monotonic()
    try:
        raw = await handler(params)
        duration = int((time.monotonic() - t0) * 1000)
        return ToolResult.ok(output=str(raw), duration_ms=duration)
    except Exception as e:
        duration = int((time.monotonic() - t0) * 1000)
        tb = tb_mod.format_exc()
        return ToolResult.error(
            output=f"{type(e).__name__}: {e}\n{tb[-800:]}",
            duration_ms=duration
        )


async def run_agent_loop(
    user_text: str,
    notify: Callable[[str], Awaitable[None]],
    conversation_id: int | None = None,
) -> str:
    """
    Run the ReAct agent loop for a user request.
    Returns the final summary string to be sent to the user.
    conversation_id: the conversations.id for the user turn, used to link tool_calls records.
    """
    # Build manifest text with param hints — builtins from manifest.json, plugins from registry
    from tools import registry as _registry
    try:
        with open("/opt/materia/manifest.json") as f:
            _manifest = json.load(f)
        lines = []
        for t in _manifest["tools"]:
            params_hint = ""
            if t.get("params"):
                hints = ", ".join(f"{k}: {v}" for k, v in t["params"].items())
                params_hint = f" | params: {hints}"
            lines.append(f"- {t['name']}: {t['description']}{params_hint}")
        for spec in _registry.all_tools():
            params_hint = ""
            if spec.params:
                hints = ", ".join(f"{k}: {v}" for k, v in spec.params.items())
                params_hint = f" | params: {hints}"
            lines.append(f"- {spec.name}: {spec.description}{params_hint}")
        manifest_text = "\n".join(lines)
    except Exception:
        manifest_text = "unavailable"

    from pathlib import Path
    scripts_dir = Path("/opt/materia/scripts")
    scripts = sorted(p.name for p in scripts_dir.glob("*.py")) if scripts_dir.exists() else []
    scripts_list = ", ".join(scripts) if scripts else "none"

    system_prompt = _build_agent_system_prompt(manifest_text, scripts_list)

    # Load recent history for context
    recent = await mem.conversation_get(limit=4)
    history_msgs = [{"role": m["role"], "content": m["content"]} for m in recent
                    if m["role"] in ("user", "assistant")]

    scratchpad = (
        [{"role": "system", "content": system_prompt}]
        + history_msgs
        + [{"role": "user", "content": user_text}]
    )

    deadline = time.monotonic() + MAX_SECONDS
    final_summary = None

    for step_n in range(1, MAX_STEPS + 1):
        if time.monotonic() > deadline:
            logger.warning("[Agent] Wall-clock budget exceeded")
            return "⏱ Ran out of time before completing the task."

        # Ask the LLM for the next step
        try:
            step = await llm.llm_structured(scratchpad, STEP_SCHEMA)
        except Exception as e:
            logger.error(f"[Agent] LLM call failed at step {step_n}: {e}")
            return f"❌ Agent LLM call failed at step {step_n}: {e}"

        action = step.get("action", "finish")
        thought = step.get("thought", "")
        logger.info(
            f"[Agent] Step {step_n}: action={action} tool={step.get('tool')} "
            f"thought={thought[:80]}"
        )

        if action == "finish":
            final_summary = step.get("summary") or "(no summary)"
            break

        tool_name = step.get("tool", "")
        tool_params = step.get("params") or {}

        # Notify user with step milestone
        detail = ""
        if getattr(config, "AGENT_VERBOSE_STEPS", True) and tool_params:
            raw_val = str(tool_params.get("raw", "")).replace("\n", " ").strip()
            if raw_val:
                truncated = raw_val[:80] + ("…" if len(raw_val) > 80 else "")
                detail = f"\n```\n{truncated}\n```"
        await notify(f"⚙️ Step {step_n} · `{tool_name}`{detail}")

        # Execute the tool
        result = await _execute_tool(tool_name, tool_params)

        # Persist tool call record linked to this conversation turn
        if conversation_id is not None:
            try:
                await mem.conversation_add_tool_call(
                    conversation_id=conversation_id,
                    step_index=step_n,
                    tool=tool_name,
                    params=tool_params,
                    status=result.status,
                    output=result.output[:2000],
                    duration_ms=result.metadata.get("duration_ms"),
                )
            except Exception as e:
                logger.warning(f"[Agent] Failed to persist tool call: {e}")

        # Feed the tool call + observation back into the scratchpad
        scratchpad.append({
            "role": "assistant",
            "content": json.dumps({
                "thought": thought,
                "action": "tool_call",
                "tool": tool_name,
                "params": tool_params,
                "summary": ""
            })
        })
        status_icon = "✅" if result.status == "ok" else "❌"
        observation = f"{status_icon} {tool_name} result:\n{result.output[:1500]}"
        scratchpad.append({"role": "user", "content": f"Observation:\n{observation}"})

    if final_summary is None:
        return f"⚠️ Reached the {MAX_STEPS}-step limit without completing the task."

    return final_summary
