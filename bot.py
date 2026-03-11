import asyncio
import logging
import json
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, BotCommand
from aiogram.filters import Command
from apscheduler.schedulers.asyncio import AsyncIOScheduler

import config
import llm
import memory as mem
import context as ctx
from intent import classify_intent
from router import route

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [Materia] %(levelname)s %(message)s"
)
logger = logging.getLogger(__name__)

bot = Bot(token=config.TELEGRAM_BOT_TOKEN)
dp = Dispatcher()
scheduler = AsyncIOScheduler(timezone=config.TIMEZONE)


async def refresh_commands():
    """Rebuild the Telegram command menu from manifest.json."""
    # Fixed meta-commands always at the top
    commands = [
        BotCommand(command="help",    description="Show commands and available tools"),
        BotCommand(command="context", description="Show token usage breakdown"),
        BotCommand(command="compact", description="Force context compaction"),
        BotCommand(command="tools",   description="List all available tools"),
        BotCommand(command="scripts", description="List scripts and schedules"),
    ]
    try:
        with open("/opt/tgbot/manifest.json") as f:
            data = json.load(f)
        for tool in data["tools"]:
            name = tool["name"]
            desc = tool["description"]
            # Telegram: command 1–32 chars (a-z, 0-9, _), description 3–256 chars
            if len(name) <= 32 and len(desc) >= 3:
                commands.append(BotCommand(command=name, description=desc[:256]))
    except Exception as e:
        logger.warning(f"Could not load manifest for command menu: {e}")
    await bot.set_my_commands(commands)
    logger.info(f"[Materia] Command menu updated — {len(commands)} entries.")


def truncate(text: str, limit: int = 4096) -> str:
    if len(text) <= limit:
        return text
    return text[:limit - 15] + "... (truncated)"


@dp.message(Command("start", "help"))
async def cmd_help(message: Message):
    if message.from_user.id not in config.TELEGRAM_ALLOWED_USERS:
        return
    with open("/opt/tgbot/manifest.json") as f:
        data = json.load(f)
    tools_list = "\n".join(f"• {t['name']} — {t['description']}" for t in data["tools"])
    await message.answer(
        "Materia — *Small spells. Real magic.*\n\n"
        "Commands:\n"
        "/context — token usage breakdown\n"
        "/compact — force context compaction\n"
        "/tools — list available tools\n"
        "/scripts — list user scripts\n"
        "/help — this message\n\n"
        f"Available tools:\n{tools_list}",
        parse_mode="Markdown"
    )


@dp.message(Command("context"))
async def cmd_context(message: Message):
    if message.from_user.id not in config.TELEGRAM_ALLOWED_USERS:
        return
    history = await mem.conversation_get_all()
    memory_data = await mem.memory_get_all()
    history_tokens = ctx.count_messages_tokens(history)
    memory_tokens = ctx.count_tokens(str(memory_data))
    system_tokens = 300
    usage = ctx.ContextUsage(
        system_tokens=system_tokens,
        memory_tokens=memory_tokens,
        history_tokens=history_tokens,
        message_tokens=0,
        total=system_tokens + memory_tokens + history_tokens,
        limit=config.CONTEXT_LIMIT
    )
    await message.answer(f"```\n{usage.format_report()}\n```", parse_mode="Markdown")


@dp.message(Command("compact"))
async def cmd_compact(message: Message):
    if message.from_user.id not in config.TELEGRAM_ALLOWED_USERS:
        return
    await message.answer("Compacting context...")
    await ctx.compact()
    await message.answer("Context compacted. Summary saved to memory.")


@dp.message(Command("tools"))
async def cmd_tools(message: Message):
    if message.from_user.id not in config.TELEGRAM_ALLOWED_USERS:
        return
    from tools.builtin import list_tools
    result = await list_tools({})
    await message.answer(result)


@dp.message(Command("scripts"))
async def cmd_scripts(message: Message):
    if message.from_user.id not in config.TELEGRAM_ALLOWED_USERS:
        return
    from tools.builtin import list_scripts
    result = await list_scripts({})
    await message.answer(result)


async def _keep_typing(chat_id: int):
    """Re-send typing indicator every 4s until cancelled. Telegram's indicator
    expires after ~5s so we refresh it to cover long intent + generation time."""
    try:
        while True:
            await bot.send_chat_action(chat_id, "typing")
            await asyncio.sleep(4)
    except asyncio.CancelledError:
        pass


async def _stream_chat(message: Message, params: dict) -> str:
    """Stream a chat response to Telegram, editing a placeholder as tokens arrive."""
    from tools.builtin import build_chat_messages
    messages = await build_chat_messages(params)

    sent = await message.answer("▌")
    accumulated = ""
    last_edit = 0  # Zero forces an edit on the very first token
    MIN_EDIT_INTERVAL = 0.5  # seconds — safe within Telegram rate limits

    try:
        async for chunk in llm.llm_stream(messages, max_tokens=config.LLM_MAX_TOKENS):
            accumulated += chunk
            now = asyncio.get_event_loop().time()
            if now - last_edit >= MIN_EDIT_INTERVAL:
                try:
                    await sent.edit_text(truncate(accumulated + "▌"))
                    last_edit = now
                except Exception:
                    pass
    except Exception as e:
        logger.error(f"Streaming error: {e}")

    # Final edit — remove cursor, show complete text
    final = truncate(accumulated) if accumulated else "[No response]"
    try:
        await sent.edit_text(final)
    except Exception:
        pass

    return accumulated


@dp.message(F.text)
async def handle_message(message: Message):
    if message.from_user.id not in config.TELEGRAM_ALLOWED_USERS:
        return

    user_text = message.text or ""
    logger.info(f"[Materia] Message from {message.from_user.id}: {user_text[:80]}")

    # Start typing indicator immediately and keep it alive for the full duration
    typing_task = asyncio.create_task(_keep_typing(message.chat.id))

    try:
        await mem.conversation_add("user", user_text)
        await ctx.check_and_compact()

        intent = await classify_intent(user_text)
        action = intent.get("action", "chat")
        params = intent.get("params", {})
        if not params.get("raw") and not params.get("query"):
            params["raw"] = user_text

        # Always show intent classification so routing is visible
        reasoning = intent.get("reasoning", "")
        await message.answer(
            f"🔀 *Intent:* `{action}`\n"
            f"*Params:* `{params}`\n"
            f"*Reasoning:* {reasoning}",
            parse_mode="Markdown"
        )

        if action == "chat":
            result = await _stream_chat(message, params)
        else:
            if action in ("create_script", "create_tool"):
                # Pass a notify callback so the tool can send progress updates
                async def _notify(text: str):
                    await message.answer(truncate(text), parse_mode="Markdown")
                params["notify"] = _notify

            result = await route(intent, user_text)
            parse_mode = "Markdown" if action in ("hn_briefing", "create_script") else None
            await message.answer(truncate(result), parse_mode=parse_mode)
            if action == "create_tool":
                await refresh_commands()

        await mem.conversation_add("assistant", result)
    finally:
        typing_task.cancel()


async def main():
    await mem.init_db()
    logger.info("[Materia] Starting up — Small spells. Real magic.")

    Path("/opt/tgbot/scripts").mkdir(exist_ok=True)
    Path("/opt/tgbot/data").mkdir(exist_ok=True)

    await refresh_commands()
    logger.info("[Materia] Bot commands registered.")

    scheduler.start()
    logger.info("[Materia] Scheduler started.")

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
