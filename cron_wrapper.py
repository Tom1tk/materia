#!/usr/bin/env python3
"""Cron wrapper for Materia — runs a script, logs the run, and alerts on failure."""
import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
load_dotenv("/opt/materia/.env")

import aiosqlite

DB_PATH = "/opt/materia/data/memory.db"
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_ALLOWED_USERS = [
    int(x.strip())
    for x in os.environ.get("TELEGRAM_ALLOWED_USERS", "").split(",")
    if x.strip().isdigit()
]


async def _log_run(script_name, exit_code, stdout, stderr, duration_ms):
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT INTO script_runs (script_name, triggered_by, exit_code, stdout, stderr, duration_ms) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (script_name, "cron", exit_code, stdout[:4000], stderr[:2000], duration_ms)
            )
            await db.commit()
    except Exception as e:
        print(f"[cron_wrapper] DB log failed: {e}", file=sys.stderr)


async def _send_telegram(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_ALLOWED_USERS:
        return
    import aiohttp
    for user_id in TELEGRAM_ALLOWED_USERS:
        try:
            async with aiohttp.ClientSession() as session:
                await session.post(
                    f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                    json={"chat_id": user_id, "text": text, "parse_mode": "HTML"},
                    timeout=aiohttp.ClientTimeout(total=15),
                )
        except Exception as e:
            print(f"[cron_wrapper] Telegram notify failed: {e}", file=sys.stderr)


async def main():
    if len(sys.argv) < 2:
        print("Usage: cron_wrapper.py <script_path>", file=sys.stderr)
        sys.exit(1)

    script_path = Path(sys.argv[1])
    script_name = script_path.name

    if not script_path.exists():
        await _send_telegram(f"<b>[Cron] ❌ Script not found</b>\n<code>{script_name}</code>")
        sys.exit(1)

    t0 = time.monotonic()
    script_env = {
        **os.environ,
        "PATH": "/opt/materia/venv/bin:/usr/local/bin:/usr/bin:/bin",
        "PYTHONIOENCODING": "utf-8",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }
    prlimit = "/usr/bin/prlimit"
    sandbox_as = str(512 * 1024 * 1024)
    if os.path.exists(prlimit):
        run_cmd = [prlimit, f"--as={sandbox_as}", "--cpu=295", "--",
                   "/opt/materia/venv/bin/python", str(script_path)]
    else:
        run_cmd = ["/opt/materia/venv/bin/python", str(script_path)]
    try:
        result = subprocess.run(
            run_cmd,
            capture_output=True, text=True, timeout=300,
            env=script_env,
        )
        exit_code = result.returncode
        stdout = result.stdout
        stderr = result.stderr
    except subprocess.TimeoutExpired:
        exit_code, stdout, stderr = 1, "", "Script timed out after 300 seconds."
    except Exception as e:
        exit_code, stdout, stderr = 1, "", f"Wrapper error: {e}"

    duration_ms = int((time.monotonic() - t0) * 1000)
    await _log_run(script_name, exit_code, stdout, stderr, duration_ms)

    if exit_code != 0:
        snippet = (stderr or stdout)[:500].strip()
        if snippet:
            msg = f"<b>[Cron] ❌ {script_name} failed</b> (exit {exit_code})\n<pre>{snippet}</pre>"
        else:
            msg = f"<b>[Cron] ❌ {script_name} failed</b> (exit {exit_code})"
        await _send_telegram(msg)
        sys.exit(exit_code)


if __name__ == "__main__":
    asyncio.run(main())
