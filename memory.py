import aiosqlite
import json
import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)
DB_PATH = "/opt/tgbot/data/memory.db"

async def init_db():
    Path("/opt/tgbot/data").mkdir(exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS memory (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS conversations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS session (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                expires_at TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS context_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                system_tokens INTEGER,
                memory_tokens INTEGER,
                history_tokens INTEGER,
                message_tokens INTEGER,
                total INTEGER,
                limit_val INTEGER,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        await db.commit()

async def memory_set(key: str, value: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO memory (key, value, updated_at) VALUES (?, ?, ?)",
            (key, value, datetime.utcnow().isoformat())
        )
        await db.commit()

async def memory_get(key: str) -> str | None:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT value FROM memory WHERE key = ?", (key,)) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else None

async def memory_get_all() -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT key, value FROM memory ORDER BY updated_at DESC") as cursor:
            rows = await cursor.fetchall()
            return {row[0]: row[1] for row in rows}

async def conversation_add(role: str, content: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO conversations (role, content) VALUES (?, ?)",
            (role, content)
        )
        await db.commit()

async def conversation_get(limit: int = 8) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT role, content FROM conversations ORDER BY id DESC LIMIT ?",
            (limit,)
        ) as cursor:
            rows = await cursor.fetchall()
            return [{"role": r[0], "content": r[1]} for r in reversed(rows)]

async def conversation_get_all() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT role, content FROM conversations ORDER BY id ASC"
        ) as cursor:
            rows = await cursor.fetchall()
            return [{"role": r[0], "content": r[1]} for r in rows]

async def conversation_clear(keep_last: int = 2):
    async with aiosqlite.connect(DB_PATH) as db:
        if keep_last > 0:
            async with db.execute(
                "SELECT id FROM conversations ORDER BY id DESC LIMIT ?",
                (keep_last,)
            ) as cursor:
                ids = [r[0] for r in await cursor.fetchall()]
            if ids:
                placeholders = ",".join("?" * len(ids))
                await db.execute(f"DELETE FROM conversations WHERE id NOT IN ({placeholders})", ids)
            else:
                await db.execute("DELETE FROM conversations")
        else:
            await db.execute("DELETE FROM conversations")
        await db.commit()

async def session_set(key: str, value: str, expires_at=None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO session (key, value, expires_at) VALUES (?, ?, ?)",
            (key, value, expires_at)
        )
        await db.commit()

async def session_get(key: str) -> str | None:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT value FROM session WHERE key = ? AND (expires_at IS NULL OR expires_at > datetime('now'))",
            (key,)
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else None

async def context_log_save(system_tokens, memory_tokens, history_tokens, message_tokens, total, limit_val):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO context_log (system_tokens, memory_tokens, history_tokens, message_tokens, total, limit_val) VALUES (?,?,?,?,?,?)",
            (system_tokens, memory_tokens, history_tokens, message_tokens, total, limit_val)
        )
        await db.commit()
