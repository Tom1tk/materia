import asyncio
import html
import logging
import json
import os
import re
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, BotCommand, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.filters import Command
from apscheduler.schedulers.asyncio import AsyncIOScheduler

import config
import llm
import memory as mem
import context as ctx
import agent
from intent import classify_intent
from router import route

_STARTED_AT = datetime.now(ZoneInfo(config.TIMEZONE))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [Materia] %(levelname)s %(message)s"
)
logger = logging.getLogger(__name__)

bot = Bot(token=config.TELEGRAM_BOT_TOKEN)
dp = Dispatcher()
scheduler = AsyncIOScheduler(timezone=config.TIMEZONE)

# Tracks the active handler task per chat so /cancel can kill it
_active_tasks: dict[int, asyncio.Task] = {}

# Pending destructive-action confirmations keyed by chat_id
_pending_confirms: dict[int, dict] = {}


async def refresh_commands():
    """Rebuild the Telegram command menu from manifest.json."""
    # Fixed meta-commands always at the top
    commands = [
        BotCommand(command="help",    description="Show commands and available tools"),
        BotCommand(command="status",  description="LLM health, model, disk, uptime"),
        BotCommand(command="cancel",  description="Cancel the current running request"),
        BotCommand(command="context", description="Show token usage breakdown"),
        BotCommand(command="compact", description="Force context compaction"),
        BotCommand(command="memory",  description="View all stored facts"),
        BotCommand(command="tools",   description="List all available tools"),
        BotCommand(command="scripts", description="List scripts and schedules"),
        BotCommand(command="reset",   description="Clear conversation history"),
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


def _md_to_tg(text: str) -> str:
    """Convert LLM markdown to Telegram Markdown v1 (bold only, no headers)."""
    # ## Heading → *Heading*
    text = re.sub(r'^#{1,6} +(.+)$', r'*\1*', text, flags=re.MULTILINE)
    # **bold** → *bold*
    text = re.sub(r'\*\*(.+?)\*\*', r'*\1*', text)
    return text


@dp.message(Command("start", "help"))
async def cmd_help(message: Message):
    if message.from_user.id not in config.TELEGRAM_ALLOWED_USERS:
        return
    with open("/opt/tgbot/manifest.json") as f:
        data = json.load(f)
    tools_list = "\n".join(
        f"• {html.escape(t['name'])} — {html.escape(t['description'])}" for t in data["tools"]
    )
    await message.answer(
        "<b>Materia</b> — Small spells. Real magic.\n\n"
        "Commands:\n"
        "/status — LLM health, model, disk, uptime\n"
        "/context — token usage breakdown\n"
        "/compact — force context compaction\n"
        "/memory — view all stored facts\n"
        "/tools — list available tools\n"
        "/scripts — list user scripts\n"
        "/reset — clear conversation history\n"
        "/cancel — cancel current request\n"
        "/help — this message\n\n"
        f"Available tools:\n{tools_list}",
        parse_mode="HTML"
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


@dp.message(Command("cancel"))
async def cmd_cancel(message: Message):
    if message.from_user.id not in config.TELEGRAM_ALLOWED_USERS:
        return
    task = _active_tasks.get(message.chat.id)
    if task and not task.done():
        task.cancel()
        await message.answer("⛔ Cancelled.")
    else:
        await message.answer("Nothing to cancel.")


@dp.message(Command("status"))
async def cmd_status(message: Message):
    if message.from_user.id not in config.TELEGRAM_ALLOWED_USERS:
        return

    lines = [f"<b>Materia Status</b>"]

    # Model
    lines.append(f"\n<b>Model:</b> <code>{html.escape(config.LLM_MODEL)}</code>")

    # LLM connectivity
    try:
        t0 = time.monotonic()
        async with __import__("aiohttp").ClientSession() as s:
            async with s.get(
                f"{config.LLM_BASE_URL}/models",
                headers={"Authorization": "Bearer local"},
                timeout=__import__("aiohttp").ClientTimeout(total=5)
            ) as r:
                await r.json()
        latency = int((time.monotonic() - t0) * 1000)
        lines.append(f"<b>LLM:</b> ✅ reachable ({latency}ms)")
    except Exception as e:
        lines.append(f"<b>LLM:</b> ❌ unreachable — {html.escape(str(e)[:80])}")

    # Context
    history = await mem.conversation_get_all()
    memory_data = await mem.memory_get_all()
    history_tokens = ctx.count_messages_tokens(history)
    memory_tokens = ctx.count_tokens(str(memory_data))
    system_tokens = 300
    total = system_tokens + memory_tokens + history_tokens
    pct = int(total / config.CONTEXT_LIMIT * 100)
    lines.append(f"<b>Context:</b> {total:,} / {config.CONTEXT_LIMIT:,} tokens ({pct}%)")

    # Disk
    disk = shutil.disk_usage("/opt/tgbot")
    used_gb = disk.used / 1024**3
    total_gb = disk.total / 1024**3
    lines.append(f"<b>Disk:</b> {used_gb:.1f} GB / {total_gb:.1f} GB used")

    # Uptime
    now = datetime.now(ZoneInfo(config.TIMEZONE))
    delta = now - _STARTED_AT
    h, rem = divmod(int(delta.total_seconds()), 3600)
    m = rem // 60
    lines.append(f"<b>Uptime:</b> {h}h {m}m (started {_STARTED_AT.strftime('%H:%M')})")

    await message.answer("\n".join(lines), parse_mode="HTML")


@dp.message(Command("memory"))
async def cmd_memory(message: Message):
    if message.from_user.id not in config.TELEGRAM_ALLOWED_USERS:
        return
    data = await mem.memory_get_all()
    if not data:
        await message.answer("No stored facts.")
        return
    lines = ["<b>Stored memory:</b>\n"]
    for k, v in data.items():
        lines.append(f"<code>{html.escape(k)}</code>: {html.escape(str(v))}")
    await message.answer(truncate("\n".join(lines), 4096), parse_mode="HTML")


@dp.message(Command("reset"))
async def cmd_reset(message: Message):
    if message.from_user.id not in config.TELEGRAM_ALLOWED_USERS:
        return
    await mem.conversation_clear(keep_last=0)
    await message.answer("History cleared.")


@dp.callback_query(F.data.startswith("confirm_"))
async def handle_confirm_callback(callback: CallbackQuery):
    chat_id = callback.message.chat.id
    pending = _pending_confirms.pop(chat_id, None)

    # Remove the keyboard from the prompt message
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await callback.answer()

    if not pending:
        await callback.message.reply("This confirmation has expired.")
        return

    if time.monotonic() > pending.get("expires_at", 0):
        await callback.message.reply("Confirmation expired.")
        return

    if callback.data == "confirm_no":
        await callback.message.reply("Cancelled.")
        return

    # User said yes — execute the action
    intent = pending["intent"]
    user_text = pending["user_text"]
    action = intent.get("action", "")
    params = intent.get("params", {})

    try:
        result = await route(intent, user_text)
        parse_mode = "Markdown" if action in ("hn_briefing",) else None
        await callback.message.reply(truncate(result), parse_mode=parse_mode)
        await mem.conversation_add("assistant", result)
    except Exception as e:
        logger.error(f"Confirmed action failed: {e}", exc_info=True)
        await callback.message.reply(f"Error executing action: {e}")


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
        async for chunk in llm.llm_stream(messages, max_tokens=config.LLM_MAX_TOKENS, temperature=0.3):
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


async def _process_text(message: Message, user_text: str):
    """Core pipeline: classify intent, dispatch to tool or chat, reply."""
    logger.info(f"[Materia] Message from {message.from_user.id}: {user_text[:80]}")

    typing_task = asyncio.create_task(_keep_typing(message.chat.id))
    _last_notify: list[float] = [0.0]

    async def _notify(text: str):
        gap = asyncio.get_event_loop().time() - _last_notify[0]
        if gap < 1.5:
            await asyncio.sleep(1.5 - gap)
        try:
            await message.answer(truncate(text), parse_mode="Markdown")
        except Exception:
            try:
                await message.answer(truncate(text))
            except Exception as e:
                logger.warning(f"notify send failed: {e}")
        _last_notify[0] = asyncio.get_event_loop().time()

    try:
        user_conv_id = await mem.conversation_add("user", user_text)
        await ctx.check_and_compact()

        intent = await classify_intent(user_text)
        mode = intent.get("mode", "chat")
        action = intent.get("action", "chat")
        params = intent.get("params", {})
        if not params.get("raw") and not params.get("query"):
            params["raw"] = user_text

        # Always show intent classification so routing is visible
        reasoning = intent.get("reasoning", "")
        await message.answer(
            f"🔀 <b>Intent:</b> <code>{html.escape(action)}</code>\n"
            f"<b>Reasoning:</b> {html.escape(reasoning)}",
            parse_mode="HTML"
        )

        if mode == "chat":
            result = await _stream_chat(message, params)
        elif mode == "agentic_task":
            result = await agent.run_agent_loop(
                user_text=user_text,
                notify=_notify,
                conversation_id=user_conv_id,
            )
            try:
                await message.answer(truncate(_md_to_tg(result)), parse_mode="Markdown")
            except Exception:
                await message.answer(truncate(result))
        else:
            # simple_tool path — check for destructive actions first
            from tools.builtin import needs_confirmation
            warning = needs_confirmation(action, params)
            if warning:
                kb = InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="✅ Yes, proceed", callback_data="confirm_yes"),
                    InlineKeyboardButton(text="❌ No, cancel", callback_data="confirm_no"),
                ]])
                _pending_confirms[message.chat.id] = {
                    "intent": intent,
                    "user_text": user_text,
                    "expires_at": time.monotonic() + 60,
                }
                typing_task.cancel()
                await message.answer(
                    f"⚠️ <b>Confirm:</b> {warning}",
                    parse_mode="HTML",
                    reply_markup=kb,
                )
                return

            if action in ("create_script", "create_tool", "edit_script"):
                params["notify"] = _notify

            result = await route(intent, user_text)
            parse_mode = "Markdown" if action in ("hn_briefing", "create_script", "edit_script") else None
            await message.answer(truncate(result), parse_mode=parse_mode)
            if action == "create_tool":
                await refresh_commands()

        await mem.conversation_add("assistant", result)
    except asyncio.CancelledError:
        logger.info(f"[Materia] Request cancelled by user")
        raise
    finally:
        typing_task.cancel()


@dp.message(F.text)
async def handle_message(message: Message):
    if message.from_user.id not in config.TELEGRAM_ALLOWED_USERS:
        return

    task = asyncio.create_task(_process_text(message, message.text or ""))
    _active_tasks[message.chat.id] = task
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.error(f"handle_message error: {e}", exc_info=True)
    finally:
        _active_tasks.pop(message.chat.id, None)


@dp.message(F.voice)
async def handle_voice(message: Message):
    if message.from_user.id not in config.TELEGRAM_ALLOWED_USERS:
        return

    async def _handle_voice():
        typing_task = asyncio.create_task(_keep_typing(message.chat.id))
        try:
            # Download the OGG voice file
            file_info = await bot.get_file(message.voice.file_id)
            file_path = f"/tmp/voice_{message.message_id}.ogg"
            await bot.download_file(file_info.file_path, file_path)

            # Transcribe
            from transcribe import transcribe_file
            text = await transcribe_file(file_path)
            try:
                os.unlink(file_path)
            except Exception:
                pass

            typing_task.cancel()
            # Show transcription, then run through the normal pipeline
            await message.reply(f"🎙️ <i>{html.escape(text)}</i>", parse_mode="HTML")
        except Exception as e:
            typing_task.cancel()
            logger.error(f"Voice handling error: {e}", exc_info=True)
            await message.reply(f"Could not process voice message: {e}")
            return

        # Process the transcribed text as if it were a normal message
        task = asyncio.create_task(_process_text(message, text))
        _active_tasks[message.chat.id] = task
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Voice pipeline error: {e}", exc_info=True)
        finally:
            _active_tasks.pop(message.chat.id, None)

    await _handle_voice()


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
