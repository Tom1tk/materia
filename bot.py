import asyncio
import html
import logging
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import aiohttp
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
from router import route, TOOL_MAP
from tools import registry

_STARTED_AT = datetime.now(ZoneInfo(config.TIMEZONE))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [Materia] %(levelname)s %(message)s"
)
logger = logging.getLogger(__name__)

bot = Bot(token=config.TELEGRAM_BOT_TOKEN)
dp = Dispatcher()
scheduler = AsyncIOScheduler(timezone=config.TIMEZONE)

# Tracks active handler tasks per chat so /cancel can kill all of them
_active_tasks: dict[int, set[asyncio.Task]] = {}

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
        with open("/opt/materia/manifest.json") as f:
            data = json.load(f)
        for tool in data["tools"]:
            name = tool["name"]
            desc = tool["description"]
            # Telegram: command 1–32 chars (a-z, 0-9, _), description 3–256 chars
            if len(name) <= 32 and len(desc) >= 3:
                commands.append(BotCommand(command=name, description=desc[:256]))
    except Exception as e:
        logger.warning(f"Could not load manifest for command menu: {e}")
    for spec in registry.all_tools():
        if len(spec.name) <= 32 and len(spec.description) >= 3:
            commands.append(BotCommand(command=spec.name, description=spec.description[:256]))
    await bot.set_my_commands(commands)
    logger.info(f"[Materia] Command menu updated — {len(commands)} entries.")


def truncate(text: str, limit: int = 4096) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    # Slice on byte boundary, then decode safely
    sliced = encoded[:limit - 15].decode("utf-8", errors="ignore")
    return sliced + "... (truncated)"


def _md_to_tg(text: str) -> str:
    """Convert LLM markdown to Telegram Markdown v1 (bold only, no headers)."""
    # ## Heading → *Heading*
    text = re.sub(r'^#{1,6} +(.+)$', r'*\1*', text, flags=re.MULTILINE)
    # **bold** → *bold*
    text = re.sub(r'\*\*(.+?)\*\*', r'*\1*', text)
    return text


async def _answer_safe(message: Message, text: str, parse_mode: str | None = None, **kwargs):
    """Send a message, falling back to plain text if the parse_mode causes a parse error."""
    from aiogram.exceptions import TelegramBadRequest
    if parse_mode:
        try:
            await message.answer(text, parse_mode=parse_mode, **kwargs)
            return
        except TelegramBadRequest:
            pass
    await message.answer(text, **kwargs)


@dp.message(Command("start", "help"))
async def cmd_help(message: Message):
    if not message.from_user or message.from_user.id not in config.TELEGRAM_ALLOWED_USERS:
        return
    with open("/opt/materia/manifest.json") as f:
        data = json.load(f)
    tool_entries = [f"• {html.escape(t['name'])} — {html.escape(t['description'])}" for t in data["tools"]]
    for spec in registry.all_tools():
        tool_entries.append(f"• {html.escape(spec.name)} — {html.escape(spec.description)}")
    tools_list = "\n".join(tool_entries)
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
    if not message.from_user or message.from_user.id not in config.TELEGRAM_ALLOWED_USERS:
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
    if not message.from_user or message.from_user.id not in config.TELEGRAM_ALLOWED_USERS:
        return
    await message.answer("Compacting context...")
    await ctx.compact()
    await message.answer("Context compacted. Summary saved to memory.")


@dp.message(Command("tools"))
async def cmd_tools(message: Message):
    if not message.from_user or message.from_user.id not in config.TELEGRAM_ALLOWED_USERS:
        return
    from tools.builtin import list_tools
    result = await list_tools({})
    await message.answer(result)


@dp.message(Command("scripts"))
async def cmd_scripts(message: Message):
    if not message.from_user or message.from_user.id not in config.TELEGRAM_ALLOWED_USERS:
        return
    from tools.builtin import list_scripts
    result = await list_scripts({})
    await message.answer(result)


@dp.message(Command("cancel"))
async def cmd_cancel(message: Message):
    if not message.from_user or message.from_user.id not in config.TELEGRAM_ALLOWED_USERS:
        return
    tasks = _active_tasks.pop(message.chat.id, set())
    active = [t for t in tasks if not t.done()]
    if active:
        for t in active:
            t.cancel()
        await message.answer("⛔ Cancelled.")
    else:
        await message.answer("Nothing to cancel.")


@dp.message(Command("status"))
async def cmd_status(message: Message):
    if not message.from_user or message.from_user.id not in config.TELEGRAM_ALLOWED_USERS:
        return

    lines = [f"<b>Materia Status</b>"]

    # LLM connectivity + dynamic model name
    try:
        t0 = time.monotonic()
        async with aiohttp.ClientSession() as s:
            async with s.get(
                f"{config.LLM_BASE_URL}/models",
                headers={"Authorization": "Bearer local"},
                timeout=aiohttp.ClientTimeout(total=5)
            ) as r:
                data = await r.json()
        latency = int((time.monotonic() - t0) * 1000)
        try:
            model_id = data["data"][0]["id"]
        except (KeyError, IndexError, TypeError):
            model_id = config.LLM_MODEL
        lines.append(f"\n<b>Model:</b> <code>{html.escape(model_id)}</code>")
        lines.append(f"<b>LLM:</b> ✅ reachable ({latency}ms)")
    except Exception as e:
        lines.append(f"\n<b>Model:</b> <code>{html.escape(config.LLM_MODEL)}</code>")
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
    disk = shutil.disk_usage("/opt/materia")
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
    if not message.from_user or message.from_user.id not in config.TELEGRAM_ALLOWED_USERS:
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
    if not message.from_user or message.from_user.id not in config.TELEGRAM_ALLOWED_USERS:
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
        parse_mode = "Markdown" if _is_markdown(action) else None
        from aiogram.exceptions import TelegramBadRequest
        try:
            await callback.message.reply(truncate(result), parse_mode=parse_mode)
        except TelegramBadRequest:
            await callback.message.reply(truncate(result))
        await mem.conversation_add("assistant", result)
    except Exception as e:
        logger.error(f"Confirmed action failed: {e}", exc_info=True)
        await callback.message.reply(f"Error executing action: {e}")


async def _keep_typing(chat_id: int):
    """Re-send typing indicator every 4s until cancelled. Telegram's indicator
    expires after ~5s so we refresh it to cover long intent + generation time."""
    try:
        while True:
            try:
                await bot.send_chat_action(chat_id, "typing")
            except Exception as e:
                logger.debug(f"_keep_typing send_chat_action failed: {e}")
            await asyncio.sleep(4)
    except asyncio.CancelledError:
        pass


async def _stream_chat(message: Message, params: dict) -> str:
    """Stream a chat response to Telegram, editing a placeholder as tokens arrive."""
    from tools.builtin import build_chat_messages
    messages = await build_chat_messages(params)

    sent = await message.answer("▌")
    accumulated = ""
    last_edit = float("-inf")
    MIN_EDIT_INTERVAL = 0.5  # seconds — safe within Telegram rate limits

    try:
        async for chunk in llm.llm_stream(messages, max_tokens=config.LLM_MAX_TOKENS, temperature=0.3):
            accumulated += chunk
            now = asyncio.get_running_loop().time()
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
        gap = asyncio.get_running_loop().time() - _last_notify[0]
        if gap < 1.5:
            await asyncio.sleep(1.5 - gap)
        try:
            await message.answer(truncate(text), parse_mode="Markdown")
        except Exception:
            try:
                await message.answer(truncate(text))
            except Exception as e:
                logger.warning(f"notify send failed: {e}")
        _last_notify[0] = asyncio.get_running_loop().time()

    try:
        user_conv_id = await mem.conversation_add("user", user_text)
        await ctx.check_and_compact()

        intent = await classify_intent(user_text, before_id=user_conv_id)
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
            params["_before_id"] = user_conv_id
            result = await _stream_chat(message, params)
        elif mode == "agentic_task":
            result = await agent.run_agent_loop(
                user_text=user_text,
                notify=_notify,
                conversation_id=user_conv_id,
            )
            await _answer_safe(message, truncate(_md_to_tg(result)), parse_mode="Markdown")
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

            _spec = registry.get(action)
            if action in _BUILTIN_NOTIFY or (_spec and _spec.streams_progress):
                params["notify"] = _notify

            result = await route(intent, user_text)
            parse_mode = "Markdown" if _is_markdown(action) else None
            await _answer_safe(message, truncate(result), parse_mode=parse_mode)
            if action == "create_tool" or (_spec and _spec.refresh_commands_after):
                await refresh_commands()

        await mem.conversation_add("assistant", result)
    except asyncio.CancelledError:
        logger.info(f"[Materia] Request cancelled by user")
        raise
    finally:
        typing_task.cancel()


_SLASH_META = {
    "start", "help", "status", "context", "compact",
    "memory", "tools", "scripts", "reset", "cancel",
}

_BUILTIN_MARKDOWN = {"hn_briefing", "create_script", "edit_script"}
_BUILTIN_NOTIFY = {"create_script", "create_tool", "edit_script"}


def _is_markdown(action: str) -> bool:
    if action in _BUILTIN_MARKDOWN:
        return True
    spec = registry.get(action)
    return bool(spec and spec.markdown)


async def _run_slash_tool(message: Message, action: str, params: dict, user_text: str):
    """Dispatch a tool directly from a slash command, bypassing intent classification."""
    typing_task = asyncio.create_task(_keep_typing(message.chat.id))
    _last_notify: list[float] = [0.0]

    async def _notify(text: str):
        gap = asyncio.get_running_loop().time() - _last_notify[0]
        if gap < 1.5:
            await asyncio.sleep(1.5 - gap)
        try:
            await message.answer(truncate(text), parse_mode="Markdown")
        except Exception:
            try:
                await message.answer(truncate(text))
            except Exception as e:
                logger.warning(f"slash notify failed: {e}")
        _last_notify[0] = asyncio.get_running_loop().time()

    intent = {"action": action, "params": params, "mode": "simple_tool"}

    try:
        await mem.conversation_add("user", user_text)

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
            await message.answer(f"⚠️ <b>Confirm:</b> {warning}", parse_mode="HTML", reply_markup=kb)
            return

        _spec = registry.get(action)
        if action in _BUILTIN_NOTIFY or (_spec and _spec.streams_progress):
            params["notify"] = _notify

        result = await route(intent, user_text)
        parse_mode = "Markdown" if _is_markdown(action) else None
        await _answer_safe(message, truncate(result), parse_mode=parse_mode)

        await mem.conversation_add("assistant", result)

        if action == "create_tool" or (_spec and _spec.refresh_commands_after):
            await refresh_commands()

    except asyncio.CancelledError:
        raise
    finally:
        typing_task.cancel()


@dp.message(F.text.startswith("/"))
async def handle_slash_tool(message: Message):
    if not message.from_user or message.from_user.id not in config.TELEGRAM_ALLOWED_USERS:
        return

    text = message.text or ""
    parts = text.lstrip("/").split(None, 1)
    cmd = parts[0].split("@")[0].lower()
    args = parts[1].strip() if len(parts) > 1 else ""

    if cmd in _SLASH_META:
        return

    import sys
    in_tool_map = cmd in TOOL_MAP
    in_user_tools = bool(getattr(sys.modules.get("tools.user_tools"), cmd, None))
    in_registry = registry.get(cmd) is not None
    if not in_tool_map and not in_user_tools and not in_registry:
        await message.answer(f"Unknown tool: /{cmd}\nType /tools to see what's available.")
        return

    params = {"raw": args} if args else {}
    task = asyncio.create_task(_run_slash_tool(message, cmd, params, text))
    _active_tasks.setdefault(message.chat.id, set()).add(task)
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.error(f"handle_slash_tool error: {e}", exc_info=True)
    finally:
        _active_tasks.get(message.chat.id, set()).discard(task)


@dp.message(F.text)
async def handle_message(message: Message):
    if not message.from_user or message.from_user.id not in config.TELEGRAM_ALLOWED_USERS:
        return

    task = asyncio.create_task(_process_text(message, message.text or ""))
    _active_tasks.setdefault(message.chat.id, set()).add(task)
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.error(f"handle_message error: {e}", exc_info=True)
    finally:
        _active_tasks.get(message.chat.id, set()).discard(task)


@dp.message(F.voice)
async def handle_voice(message: Message):
    if not message.from_user or message.from_user.id not in config.TELEGRAM_ALLOWED_USERS:
        return

    async def _handle_voice():
        typing_task = asyncio.create_task(_keep_typing(message.chat.id))
        try:
            # Download the OGG voice file to a unique temp path
            file_info = await bot.get_file(message.voice.file_id)
            with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
                file_path = tmp.name
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
        _active_tasks.setdefault(message.chat.id, set()).add(task)
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Voice pipeline error: {e}", exc_info=True)
        finally:
            _active_tasks.get(message.chat.id, set()).discard(task)

    await _handle_voice()


async def main():
    await mem.init_db()
    from tools import registry
    registry.discover()
    logger.info("[Materia] Starting up — Small spells. Real magic.")

    Path("/opt/materia/scripts").mkdir(exist_ok=True)
    Path("/opt/materia/data").mkdir(exist_ok=True)

    await refresh_commands()
    logger.info("[Materia] Bot commands registered.")

    for uid in config.TELEGRAM_ALLOWED_USERS:
        try:
            await bot.send_message(uid, "✅ Bot is online.")
        except Exception as e:
            logger.warning(f"[Materia] Startup notify failed for {uid}: {e}")

    scheduler.start()
    logger.info("[Materia] Scheduler started.")

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
