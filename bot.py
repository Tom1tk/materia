import asyncio
import logging
import json
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message
from aiogram.filters import Command
from apscheduler.schedulers.asyncio import AsyncIOScheduler

import config
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


@dp.message(F.text)
async def handle_message(message: Message):
    if message.from_user.id not in config.TELEGRAM_ALLOWED_USERS:
        return

    user_text = message.text or ""
    logger.info(f"[Materia] Message from {message.from_user.id}: {user_text[:80]}")

    # Store user message in history
    await mem.conversation_add("user", user_text)

    # Check if compaction is needed
    await ctx.check_and_compact()

    # Typing indicator
    try:
        await bot.send_chat_action(message.chat.id, "typing")
    except Exception:
        pass

    # Classify intent
    intent = await classify_intent(user_text)

    # Route to handler
    result = await route(intent, user_text)

    # Store assistant response
    await mem.conversation_add("assistant", result)

    # Send reply
    reply = truncate(result)
    await message.answer(reply)


async def main():
    await mem.init_db()
    logger.info("[Materia] Starting up — Small spells. Real magic.")

    Path("/opt/tgbot/scripts").mkdir(exist_ok=True)
    Path("/opt/tgbot/data").mkdir(exist_ok=True)

    scheduler.start()
    logger.info("[Materia] Scheduler started.")

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
