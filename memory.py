import aiosqlite
import json
import logging
import time
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
            CREATE TABLE IF NOT EXISTS tool_calls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id INTEGER,
                step_index INTEGER NOT NULL DEFAULT 0,
                tool TEXT NOT NULL,
                params TEXT NOT NULL,
                status TEXT NOT NULL,
                output TEXT NOT NULL,
                duration_ms INTEGER,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (conversation_id) REFERENCES conversations(id)
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
            CREATE TABLE IF NOT EXISTS script_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                script_name TEXT NOT NULL,
                triggered_by TEXT NOT NULL,
                exit_code INTEGER,
                stdout TEXT,
                stderr TEXT,
                duration_ms INTEGER,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS script_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                script_name TEXT NOT NULL,
                content TEXT NOT NULL,
                action TEXT NOT NULL,
                description TEXT,
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

async def conversation_add(role: str, content: str) -> int:
    """Insert a conversation turn and return its row id."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "INSERT INTO conversations (role, content) VALUES (?, ?)",
            (role, content)
        )
        await db.commit()
        return cursor.lastrowid

async def conversation_add_tool_call(
    conversation_id: int,
    step_index: int,
    tool: str,
    params: dict,
    status: str,
    output: str,
    duration_ms: int | None = None,
):
    """Record a tool call associated with a conversation turn."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO tool_calls (conversation_id, step_index, tool, params, status, output, duration_ms) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (conversation_id, step_index, tool, json.dumps(params), status, output, duration_ms)
        )
        await db.commit()

async def conversation_get(limit: int = 8, include_tools: bool = False) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, role, content FROM conversations ORDER BY id DESC LIMIT ?",
            (limit,)
        ) as cursor:
            rows = await cursor.fetchall()
        turns = [{"id": r[0], "role": r[1], "content": r[2]} for r in reversed(rows)]

        if not include_tools:
            return [{"role": t["role"], "content": t["content"]} for t in turns]

        # Interleave tool_calls for the returned conversation ids
        ids = [t["id"] for t in turns]
        if not ids:
            return []
        placeholders = ",".join("?" * len(ids))
        async with db.execute(
            f"SELECT conversation_id, step_index, tool, params, status, output "
            f"FROM tool_calls WHERE conversation_id IN ({placeholders}) ORDER BY id ASC",
            ids
        ) as cursor:
            tool_rows = await cursor.fetchall()

        # Build a map from conv_id → list of tool messages
        from collections import defaultdict
        tool_map = defaultdict(list)
        for conv_id, step_idx, tool, params_json, status, output in tool_rows:
            prefix = "✅" if status == "ok" else "❌"
            tool_map[conv_id].append({
                "role": "tool",
                "content": f"{prefix} Step {step_idx} · {tool}\n{output}"
            })

        result = []
        for turn in turns:
            result.append({"role": turn["role"], "content": turn["content"]})
            result.extend(tool_map.get(turn["id"], []))
        return result

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


# ─── Script Run History ──────────────────────────────────────────────────────

async def script_run_log(
    script_name: str,
    triggered_by: str,
    exit_code: int,
    stdout: str,
    stderr: str,
    duration_ms: int,
):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO script_runs (script_name, triggered_by, exit_code, stdout, stderr, duration_ms) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (script_name, triggered_by, exit_code, stdout[:4000], stderr[:2000], duration_ms)
        )
        await db.commit()


async def script_run_history(script_name: str | None = None, limit: int = 10) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        if script_name:
            async with db.execute(
                "SELECT script_name, triggered_by, exit_code, stdout, stderr, duration_ms, timestamp "
                "FROM script_runs WHERE script_name LIKE ? ORDER BY id DESC LIMIT ?",
                (f"%{script_name}%", limit)
            ) as cursor:
                rows = await cursor.fetchall()
        else:
            async with db.execute(
                "SELECT script_name, triggered_by, exit_code, stdout, stderr, duration_ms, timestamp "
                "FROM script_runs ORDER BY id DESC LIMIT ?",
                (limit,)
            ) as cursor:
                rows = await cursor.fetchall()
    return [
        {
            "script_name": r[0],
            "triggered_by": r[1],
            "exit_code": r[2],
            "stdout": r[3],
            "stderr": r[4],
            "duration_ms": r[5],
            "timestamp": r[6],
        }
        for r in rows
    ]


# ─── Script Versioning ───────────────────────────────────────────────────────

async def script_version_save(
    script_name: str,
    content: str,
    action: str,
    description: str | None = None,
):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO script_versions (script_name, content, action, description) VALUES (?, ?, ?, ?)",
            (script_name, content, action, description)
        )
        await db.commit()


async def script_version_list(script_name: str, limit: int = 10) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, script_name, action, description, timestamp FROM script_versions "
            "WHERE script_name = ? ORDER BY id DESC LIMIT ?",
            (script_name, limit)
        ) as cursor:
            rows = await cursor.fetchall()
    return [
        {"id": r[0], "script_name": r[1], "action": r[2], "description": r[3], "timestamp": r[4]}
        for r in rows
    ]


async def script_version_get(version_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, script_name, content, action, description, timestamp "
            "FROM script_versions WHERE id = ?",
            (version_id,)
        ) as cursor:
            row = await cursor.fetchone()
    if not row:
        return None
    return {
        "id": row[0], "script_name": row[1], "content": row[2],
        "action": row[3], "description": row[4], "timestamp": row[5],
    }
