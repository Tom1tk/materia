import asyncio
import importlib
import json
import logging
import os
import subprocess
from datetime import datetime
from pathlib import Path

import aiohttp

import config
import llm
import memory as mem
import sys

logger = logging.getLogger(__name__)

SCRIPTS_DIR = Path("/opt/tgbot/scripts")
MANIFEST_PATH = Path("/opt/tgbot/manifest.json")


# ─── 1. CHAT ────────────────────────────────────────────────────────────────

async def build_chat_messages(params: dict) -> list:
    """Build the message list for a chat request. Shared by chat() and streaming path."""
    history = await mem.conversation_get(limit=config.HISTORY_WINDOW)
    memory_data = await mem.memory_get_all()

    memory_text = ""
    if memory_data:
        items = list(memory_data.items())[:20]
        memory_text = "\nUser preferences:\n" + "\n".join(f"- {k}: {v}" for k, v in items)

    system = f"""You are Materia — a local-first personal assistant. Small spells. Real magic.
Be helpful, concise, and direct. British English, metric units, 24h time, ISO dates.{memory_text}"""

    messages = [{"role": "system", "content": system}] + history
    if params.get("raw"):
        messages.append({"role": "user", "content": params["raw"]})
    return messages


async def chat(params: dict) -> str:
    messages = await build_chat_messages(params)
    return await llm.llm_plain(messages, max_tokens=config.LLM_MAX_TOKENS)


# ─── 2. WEB SEARCH ──────────────────────────────────────────────────────────

async def web_search(params: dict) -> str:
    query = params.get("query", "")
    length = params.get("length", "medium")
    if not query:
        return "No search query provided."

    results = []
    if config.SEARXNG_URL:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{config.SEARXNG_URL}/search",
                    params={"q": query, "format": "json"},
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    data = await resp.json()
                    results = data.get("results", [])[:5]
        except Exception as e:
            logger.warning(f"SearXNG failed: {e}, falling back to DuckDuckGo")

    if not results:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    "https://api.duckduckgo.com/",
                    params={"q": query, "format": "json", "no_html": "1"},
                    headers={"User-Agent": "Materia/0.1"},
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    data = await resp.json(content_type=None)
                    related = data.get("RelatedTopics", [])[:5]
                    results = [{"title": r.get("Text", ""), "url": r.get("FirstURL", ""), "content": r.get("Text", "")} for r in related if isinstance(r, dict)]
        except Exception as e:
            logger.error(f"DuckDuckGo fallback failed: {e}")
            return f"Search failed for: {query}"

    if not results:
        return f"No results found for: {query}"

    context = "\n\n".join(
        f"Title: {r.get('title','')}\nURL: {r.get('url','')}\nSnippet: {r.get('content', r.get('snippet',''))}"
        for r in results
    )

    length_guide = {"short": "2-3 sentences", "medium": "1 paragraph", "long": "2-3 paragraphs"}.get(length, "1 paragraph")
    messages = [
        {"role": "system", "content": "You are Materia. Summarise web search results clearly and concisely. British English."},
        {"role": "user", "content": f"Summarise these search results for the query '{query}' in {length_guide}:\n\n{context}"}
    ]
    return await llm.llm_plain(messages, max_tokens=512)


# ─── 3. HN BRIEFING ─────────────────────────────────────────────────────────

async def hn_briefing(params: dict) -> str:
    length = params.get("length", "medium")
    topic = params.get("topic", "")
    n = {"short": 5, "medium": 10, "long": 20}.get(length, 10)

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://hacker-news.firebaseio.com/v0/topstories.json",
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                top_ids = (await resp.json())[:30]

            stories = []
            for story_id in top_ids[:n*2]:  # fetch more to filter by topic
                try:
                    async with session.get(
                        f"https://hacker-news.firebaseio.com/v0/item/{story_id}.json",
                        timeout=aiohttp.ClientTimeout(total=5)
                    ) as resp:
                        item = await resp.json()
                        if item and item.get("title"):
                            stories.append(item)
                            if len(stories) >= n:
                                break
                except Exception:
                    continue
    except Exception as e:
        return f"Failed to fetch HN stories: {e}"

    # Filter by topic if provided
    if topic:
        filtered = [s for s in stories if topic.lower() in s.get("title", "").lower()]
        if filtered:
            stories = filtered

    stories = stories[:n]
    # Store in session for drill-down
    session_data = json.dumps([{"id": s["id"], "title": s.get("title", ""), "url": s.get("url", ""), "score": s.get("score", 0), "by": s.get("by", "")} for s in stories])
    await mem.session_set("hn_current_stories", session_data)

    # Get one-line summaries as a structured array so we can pair each with metadata
    context = "\n".join(f"{i+1}. {s.get('title', '')} (by {s.get('by', '')})" for i, s in enumerate(stories))
    schema = {
        "type": "object",
        "properties": {
            "summaries": {
                "type": "array",
                "items": {"type": "string"}
            }
        },
        "required": ["summaries"],
        "additionalProperties": False
    }
    messages = [
        {"role": "system", "content": "You are Materia. For each numbered story, write a single concise sentence summarising what it is about. British English. Return exactly one summary string per story in the array, in the same order."},
        {"role": "user", "content": f"Summarise these Hacker News stories:\n{context}"}
    ]
    result = await llm.llm_structured(messages, schema)
    summaries = result.get("summaries", [])

    # Format each story: score → summary → HN link
    lines = [f"Hacker News — top {n} stories:\n"]
    for i, s in enumerate(stories):
        score = s.get("score", 0)
        hn_url = f"https://news.ycombinator.com/item?id={s['id']}"
        summary_text = summaries[i] if i < len(summaries) else s.get("title", "")
        lines.append(f"▲ {score} pts\n{summary_text}\n{hn_url}")

    return "\n\n".join(lines)


# ─── 4. CREATE SCRIPT ───────────────────────────────────────────────────────

SCRIPT_SCHEMA = {
    "type": "object",
    "properties": {
        "script": {"type": "string"},
        "filename": {"type": "string"},
        "description": {"type": "string"}
    },
    "required": ["script", "filename", "description"],
    "additionalProperties": False
}

async def create_script(params: dict) -> str:
    description = params.get("description", "")
    schedule = params.get("schedule", "")
    test_first = params.get("test_first", True)

    messages = [
        {"role": "system", "content": "You are Materia. Generate a Python script. Return JSON with script, filename (no spaces, .py extension), and description. The script must be self-contained and include all necessary imports."},
        {"role": "user", "content": f"Create a Python script that: {description}"}
    ]

    result = await llm.llm_structured(messages, SCRIPT_SCHEMA)
    script_code = result["script"]
    filename = result["filename"].replace(" ", "_")
    if not filename.endswith(".py"):
        filename += ".py"

    SCRIPTS_DIR.mkdir(exist_ok=True)
    script_path = SCRIPTS_DIR / filename
    script_path.write_text(script_code)
    os.chmod(script_path, 0o755)

    response = f"Script created: {filename}\n\nDescription: {result['description']}\n\n```python\n{script_code[:1000]}{'...(truncated)' if len(script_code) > 1000 else ''}\n```"

    if test_first:
        test_result = _run_script_sync(script_path)
        response += f"\n\nTest run output:\n```\n{test_result[:500]}\n```"
        if schedule:
            response += f"\n\nReply 'yes' to schedule this with cron: `{schedule}`"
            await mem.session_set(f"pending_cron_{filename}", schedule)
        return response

    if schedule:
        cron_result = _add_cron_entry(filename, schedule)
        response += f"\n\nScheduled: {schedule}\n{cron_result}"

    return response


def _run_script_sync(script_path: Path) -> str:
    try:
        result = subprocess.run(
            ["/opt/tgbot/venv/bin/python", str(script_path)],
            capture_output=True, text=True, timeout=30
        )
        output = result.stdout + result.stderr
        return output[:1000] or "(no output)"
    except subprocess.TimeoutExpired:
        return "Script timed out after 30 seconds."
    except Exception as e:
        return f"Error running script: {e}"


def _add_cron_entry(filename: str, schedule: str) -> str:
    try:
        result = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
        existing = result.stdout if result.returncode == 0 else ""
        entry = f"{schedule} /opt/tgbot/venv/bin/python /opt/tgbot/scripts/{filename}\n"
        if entry.strip() in existing:
            return "Cron entry already exists."
        new_crontab = existing + entry
        proc = subprocess.run(["crontab", "-"], input=new_crontab, capture_output=True, text=True)
        if proc.returncode == 0:
            return f"Cron entry added: {schedule}"
        return f"Failed to add cron: {proc.stderr}"
    except Exception as e:
        return f"Error adding cron: {e}"


# ─── 5. LIST SCRIPTS ────────────────────────────────────────────────────────

async def list_scripts(params: dict) -> str:
    SCRIPTS_DIR.mkdir(exist_ok=True)
    scripts = list(SCRIPTS_DIR.glob("*.py"))
    if not scripts:
        return "No scripts found in /opt/tgbot/scripts/"

    # Read crontab
    try:
        result = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
        crontab_text = result.stdout if result.returncode == 0 else ""
    except Exception:
        crontab_text = ""

    lines = []
    for script in sorted(scripts):
        name = script.name
        schedule = ""
        for line in crontab_text.splitlines():
            if name in line and not line.startswith("#"):
                parts = line.split()
                if len(parts) >= 5:
                    schedule = " ".join(parts[:5])
        lines.append(f"• {name}" + (f" — {schedule}" if schedule else " — (no schedule)"))

    return "Scripts:\n" + "\n".join(lines)


# ─── 6. RUN SCRIPT ──────────────────────────────────────────────────────────

async def run_script(params: dict) -> str:
    name = params.get("raw", "").strip()
    if not name:
        return "Please specify a script name."
    if not name.endswith(".py"):
        name += ".py"
    script_path = SCRIPTS_DIR / name
    if not script_path.exists():
        return f"Script not found: {name}"
    return _run_script_sync(script_path)


# ─── 7. ADD CRON ────────────────────────────────────────────────────────────

async def add_cron(params: dict) -> str:
    name = params.get("raw", "").strip()
    schedule = params.get("schedule", "").strip()
    if not name or not schedule:
        return "Please provide both a script name and a cron schedule."
    if not name.endswith(".py"):
        name += ".py"
    return _add_cron_entry(name, schedule)


# ─── 8. REMOVE CRON ─────────────────────────────────────────────────────────

async def remove_cron(params: dict) -> str:
    name = params.get("raw", "").strip()
    if not name:
        return "Please specify a script name."
    if not name.endswith(".py"):
        name += ".py"
    try:
        result = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
        if result.returncode != 0:
            return "No crontab found."
        lines = result.stdout.splitlines()
        new_lines = [l for l in lines if name not in l]
        if len(new_lines) == len(lines):
            return f"No cron entry found for {name}."
        new_crontab = "\n".join(new_lines) + "\n"
        proc = subprocess.run(["crontab", "-"], input=new_crontab, capture_output=True, text=True)
        if proc.returncode == 0:
            return f"Cron entry removed for {name}."
        return f"Failed to update crontab: {proc.stderr}"
    except Exception as e:
        return f"Error removing cron: {e}"


# ─── 9. CREATE TOOL ─────────────────────────────────────────────────────────

TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "function_code": {"type": "string"},
        "tool_name": {"type": "string"},
        "description": {"type": "string"},
        "schedule": {"type": "string"}
    },
    "required": ["function_code", "tool_name", "description", "schedule"],
    "additionalProperties": False
}

async def create_tool(params: dict) -> str:
    description = params.get("description", "")
    tool_name = params.get("tool_name", "")
    schedule = params.get("schedule", "")

    messages = [
        {"role": "system", "content": """You are Materia. Generate a Python async tool function.
Return JSON with:
- function_code: complete async Python function (include all imports at top of function body or as module-level)
- tool_name: snake_case name
- description: one-line description
- schedule: cron expression or empty string

The function signature must be: async def <tool_name>(params: dict) -> str:
It should return a string result. Import any modules inside the function body."""},
        {"role": "user", "content": f"Create a tool that: {description}" + (f"\nTool name: {tool_name}" if tool_name else "")}
    ]

    result = await llm.llm_structured(messages, TOOL_SCHEMA)
    fn_code = result["function_code"]
    name = result["tool_name"].replace("-", "_").replace(" ", "_")
    desc = result["description"]
    sched = result.get("schedule") or schedule

    # Append to user_tools.py
    user_tools_path = Path("/opt/tgbot/tools/user_tools.py")
    with open(user_tools_path, "a") as f:
        f.write(f"\n\n{fn_code}\n")

    # Add to manifest
    with open(MANIFEST_PATH) as f:
        manifest = json.load(f)
    manifest["tools"].append({
        "name": name,
        "description": desc,
        "builtin": False,
        "added": datetime.now().strftime("%Y-%m-%d")
    })
    with open(MANIFEST_PATH, "w") as f:
        json.dump(manifest, f, indent=2)

    # Hot-reload
    import tools.user_tools as user_tools_module
    importlib.reload(user_tools_module)

    return f"Tool '{name}' created and registered.\n\nDescription: {desc}\n\n```python\n{fn_code[:800]}{'...' if len(fn_code)>800 else ''}\n```"


# ─── 10. LIST TOOLS ─────────────────────────────────────────────────────────

async def list_tools(params: dict) -> str:
    with open(MANIFEST_PATH) as f:
        data = json.load(f)
    lines = []
    for t in data["tools"]:
        tag = "(built-in)" if t.get("builtin") else "(custom)"
        lines.append(f"• {t['name']} {tag} — {t['description']}")
    return "Available tools:\n" + "\n".join(lines)


# ─── 11. MEMORY SET ─────────────────────────────────────────────────────────

async def memory_set_tool(params: dict) -> str:
    raw = params.get("raw", "")
    if ":" not in raw:
        return "Format: key: value"
    key, val = raw.split(":", 1)
    await mem.memory_set(key.strip(), val.strip())
    return f"Saved: {key.strip()} = {val.strip()}"


# ─── 12. MEMORY GET ─────────────────────────────────────────────────────────

async def memory_get_tool(params: dict) -> str:
    key = params.get("raw", "").strip()
    val = await mem.memory_get(key)
    if val is None:
        return f"No memory entry found for: {key}"
    return f"{key}: {val}"
