import logging
import traceback
from tools.builtin import (
    chat, web_search, hn_briefing, create_script, list_scripts,
    run_script, add_cron, remove_cron, create_tool, list_tools,
    memory_set_tool, memory_get_tool, run_shell, edit_script
)

logger = logging.getLogger(__name__)

TOOL_MAP = {
    "chat": chat,
    "web_search": web_search,
    "hn_briefing": hn_briefing,
    "create_script": create_script,
    "list_scripts": list_scripts,
    "run_script": run_script,
    "add_cron": add_cron,
    "remove_cron": remove_cron,
    "create_tool": create_tool,
    "list_tools": list_tools,
    "memory_set": memory_set_tool,
    "memory_get": memory_get_tool,
    "run_shell": run_shell,
    "edit_script": edit_script,
}

async def route(intent: dict, user_message: str) -> str:
    action = intent.get("action", "chat")
    params = intent.get("params", {})

    # Inject raw user message if not already in params
    if not params.get("raw") and not params.get("query"):
        params["raw"] = user_message

    handler = TOOL_MAP.get(action)
    if handler is None:
        # Check user_tools via sys.modules so hot-reloaded version is used
        import sys
        user_tools_module = sys.modules.get("tools.user_tools")
        if user_tools_module:
            handler = getattr(user_tools_module, action, None)

    if handler is None:
        logger.warning(f"No handler for action: {action}, falling back to chat")
        handler = chat
        params["raw"] = user_message

    try:
        result = await handler(params)
        return result or "(no response)"
    except Exception as e:
        tb = traceback.format_exc()
        logger.error(f"Tool '{action}' failed: {tb}")
        return (
            f"❌ Tool `{action}` failed\n\n"
            f"*{type(e).__name__}:* `{e}`\n\n"
            f"```\n{tb[-1500:]}\n```"
        )
