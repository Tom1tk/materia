# Materia

> *Small spells. Real magic.*

Materia is a local-first Telegram bot running on a Proxmox server. It routes natural-language messages through an intent classifier to a suite of built-in tools, with the ability to create and drop in new tools on demand.

---

## Features

- **Intent classification** via a local LLM (llama-server / OpenAI-compatible)
- **ReAct agentic loop** — multi-step reasoning with tool use and self-correction
- **16 built-in tools**: chat, web search, Hacker News briefing, script creation/editing/versioning, cron management, shell execution, tool creation, memory, run history
- **Drop-in tool plugins** — add a tool by dropping one file in `tools/`; no core-file edits needed
- **Slash-command bypass** — `/tool_name arg` routes directly without LLM intent classification
- **Voice messages** — OGG voice notes transcribed via faster-whisper, then routed normally
- **Script versioning** — snapshots saved on create/edit; rollback to any previous version
- **Script run history** — all script executions logged; inspect per-script or across all scripts
- **Cron failure notifications** — Telegram alert sent when any scheduled script exits non-zero
- **Destructive command confirmation** — inline Yes/No keyboard for dangerous operations
- **Persistent memory** stored in SQLite (`data/memory.db`)
- **Context compaction** — summarises conversation history when the context window fills up
- **Hot-reload** of LLM-created tools without restarting the bot
- **User allowlist** — only configured Telegram user IDs can interact

---

## Requirements

- Python 3.11+
- A running llama-server instance (OpenAI-compatible `/v1` endpoint)
- A Telegram bot token (from [@BotFather](https://t.me/botfather))
- Optional: SearXNG instance for web search
- Optional: faster-whisper (for voice message transcription; installed via `requirements.txt`)

---

## Directory Structure

```
/opt/tgbot/
├── bot.py              # Entry point, Telegram dispatcher, voice handling, confirmation UI
├── agent.py            # ReAct agentic loop
├── intent.py           # LLM-based intent classifier
├── router.py           # Maps intents to tool handlers
├── memory.py           # SQLite persistence (memory, conversations, sessions, versioning)
├── context.py          # Token counting and context compaction
├── llm.py              # LLM client (structured + plain text)
├── config.py           # Environment variable loading
├── transcribe.py       # faster-whisper voice transcription
├── cron_wrapper.py     # Wraps cron scripts; sends Telegram alert on failure
├── tools/
│   ├── spec.py         # ToolSpec dataclass — the contract for drop-in tools
│   ├── registry.py     # Tool registry: register(), get(), all_tools(), discover()
│   ├── builtin.py      # All 16 built-in tools
│   ├── user_tools.py   # Hot-reloaded LLM-created tools (via create_tool)
│   └── <name>.py       # Drop-in tool files (auto-discovered on startup)
├── manifest.json       # Registry for the 16 built-in tools
├── scripts/            # User-generated Python scripts (drop-in; auto-discovered)
├── data/               # SQLite database (git-ignored)
├── .env                # Secrets (git-ignored)
├── .env.example        # Template for .env
└── requirements.txt
```

---

## Installation

```bash
# Clone or copy files to /opt/tgbot/
cd /opt/tgbot

# Create and activate virtualenv
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Configure environment
cp .env.example .env
# Edit .env with your values

# Run manually to verify
python bot.py
```

---

## Configuration

Copy `.env.example` to `.env` and fill in the values:

| Variable | Description | Default |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | Bot token from BotFather | required |
| `TELEGRAM_ALLOWED_USERS` | Comma-separated Telegram user IDs | required |
| `LLM_BASE_URL` | OpenAI-compatible LLM base URL | `http://localhost:8080/v1` |
| `LLM_MODEL` | Model name (llama-server ignores this) | `local` |
| `LLM_MAX_TOKENS` | Max tokens per LLM response | `10240` |
| `LLM_TEMPERATURE` | LLM sampling temperature | `0.7` |
| `SEARXNG_URL` | SearXNG instance URL (optional) | `` |
| `TIMEZONE` | Scheduler timezone | `Europe/London` |
| `CONTEXT_LIMIT` | Token budget for context window | `131072` |
| `COMPACTION_THRESHOLD` | Fraction at which compaction triggers | `0.65` |
| `WARN_THRESHOLD` | Fraction at which a warning is issued | `0.50` |
| `HISTORY_WINDOW` | Number of conversation turns to include | `8` |
| `AGENT_MAX_STEPS` | Maximum tool-call steps per agentic task | `100` |
| `AGENT_VERBOSE_STEPS` | Show the command run in each step notification | `true` |

---

## Systemd Service

The service runs as the `tgbot` system user.

```bash
# Enable and start
sudo systemctl enable tgbot
sudo systemctl start tgbot

# View logs
sudo journalctl -u tgbot -f

# Restart after changes
sudo systemctl restart tgbot
```

---

## Built-in Commands

| Command | Description |
|---|---|
| `/help` or `/start` | Show help and available tools |
| `/status` | Show LLM status, model name, context usage, disk, and uptime |
| `/context` | Show token usage breakdown |
| `/compact` | Force context compaction now |
| `/tools` | List all registered tools |
| `/scripts` | List scripts in `/opt/tgbot/scripts/` |
| `/memory` | Dump all stored facts from the memory table |
| `/reset` | Clear conversation history completely |
| `/cancel` | Cancel the current in-progress operation |

Any registered tool is also directly callable as a slash command:

```
/proxmox_status
/proxmox_gpu 3600
/web_search python asyncio tutorial
```

---

## Built-in Tools

| Tool | Description |
|---|---|
| `chat` | General conversation and questions |
| `web_search` | Search via SearXNG (falls back to DuckDuckGo) |
| `hn_briefing` | Hacker News top stories with optional topic filter |
| `create_script` | Generate a Python script and optionally schedule it |
| `edit_script` | Edit or fix an existing script based on instructions |
| `list_scripts` | List all scripts with their cron schedules |
| `run_script` | Manually run a script by name |
| `script_history` | Show recent run history for a script (or all scripts) |
| `rollback_script` | List versions of a script or restore to a previous one |
| `run_shell` | Run a shell command or install a package |
| `add_cron` | Add or modify a cron entry for a script |
| `remove_cron` | Remove a cron entry for a script |
| `create_tool` | Create a new tool from a plain-English description (appends to `user_tools.py`) |
| `list_tools` | List all available tools |
| `memory_set` | Save a preference or fact (`key: value`) |
| `memory_get` | Retrieve a stored preference or fact |

---

## Adding a Tool by Hand (Drop-in Pattern)

Drop a `.py` file into `tools/`. The bot discovers and registers it automatically on the next startup — no edits to `router.py`, `intent.py`, `bot.py`, or `manifest.json`.

### Minimal example

```python
# tools/bitcoin_price.py
from tools.spec import ToolSpec
from tools.registry import register

async def bitcoin_price(params: dict) -> str:
    import aiohttp
    async with aiohttp.ClientSession() as s:
        async with s.get("https://api.coindesk.com/v1/bpi/currentprice/GBP.json") as r:
            data = await r.json()
    price = data["bpi"]["GBP"]["rate"]
    return f"BTC: £{price}"

register(ToolSpec(
    name="bitcoin_price",
    description="Current Bitcoin price in GBP",
    handler=bitcoin_price,
))
```

That's it. After `sudo systemctl restart tgbot`:
- `/bitcoin_price` works as a slash command
- `list_tools` and `/help` include it
- The intent classifier can route to it (if `intent_hint` is set)

### Full ToolSpec fields

| Field | Type | Default | Purpose |
|---|---|---|---|
| `name` | `str` | required | Snake-case tool name; also the slash command (`/bitcoin_price`) |
| `description` | `str` | required | Shown in `/tools`, `/help`, command menu, and intent prompt |
| `handler` | `async def(params: dict) -> str` | required | The implementation function |
| `params` | `dict` | `{}` | Documented param shape (informational only) |
| `added` | `str` | `""` | `YYYY-MM-DD` creation date |
| `intent_hint` | `str \| None` | `None` | Routing rules block for the intent classifier. Include trigger words and how to map params. Omit to make the tool slash-only (no LLM routing). |
| `confirm` | `callable \| None` | `None` | `def(params) -> str \| None` — return an HTML warning string to require Yes/No confirmation before running; return `None` to skip. |
| `markdown` | `bool` | `False` | Render output with Telegram Markdown v1 parse_mode. |
| `streams_progress` | `bool` | `False` | Inject a `notify(text)` async callback into `params["notify"]` for live progress updates during long-running operations. |
| `refresh_commands_after` | `bool` | `False` | Refresh the Telegram command menu after a successful run (use when the tool itself creates new tools or changes the available command set). |

### Multi-tool file (shared helpers)

A single file can register multiple tools. Use this when tools share helper functions:

```python
# tools/my_service.py
from tools.spec import ToolSpec
from tools.registry import register

async def _api_get(path): ...   # shared helper

async def service_status(params): ...
async def service_control(params): ...

def _confirm_control(params):
    if params.get("description") == "stop":
        return f"Stop <code>{params.get('raw','?')}</code>?"
    return None

register(ToolSpec(name="service_status", description="...", handler=service_status, markdown=True))
register(ToolSpec(name="service_control", description="...", handler=service_control, confirm=_confirm_control))
```

### Private / personal tools

Add the file to `.gitignore` — it works identically, but stays off the repo:

```
# .gitignore
tools/my_private_tool.py
```

### Hot-reload

Drop-in tools require a service restart to be discovered:

```bash
sudo systemctl restart tgbot
```

Tools created via `create_tool` (LLM-generated) hot-reload without a restart.

---

## Creating Tools via LLM

Send a message like:

> "Create a tool that checks the current Bitcoin price and returns it in GBP"

The bot will:
1. Generate an async Python function via the LLM
2. Append it to `tools/user_tools.py`
3. Register it in `manifest.json`
4. Hot-reload the module — no restart needed

For hand-authored tools with full control over routing, confirmation, and markdown rendering, use the drop-in pattern above instead.

---

## Adding Scripts

Drop a `.py` file into `scripts/`. It is immediately:
- Runnable via `run_script` or `/run_script <name>`
- Schedulable via `add_cron`
- Visible in `list_scripts`

No registration or core-file edits needed. Scripts run via `cron_wrapper.py` which sends a Telegram alert on failure.

---

## Context Compaction

When conversation history reaches `COMPACTION_THRESHOLD` (65% of `CONTEXT_LIMIT` by default), the bot:

1. Sends the full history to the LLM for summarisation
2. Appends the summary to `MEMORY.md`
3. Extracts structured key-value facts into the SQLite `memory` table
4. Clears the conversation history, keeping only the last 2 messages

Force compaction manually with `/compact`.

---

## Voice Messages

Send an OGG voice note to the bot. It will:
1. Transcribe the audio using faster-whisper (`tiny` model, runs locally)
2. Show the transcription
3. Route the text through the normal intent → tool pipeline

---

## Script Versioning

Every `create_script` and `edit_script` call snapshots the previous version into the `script_versions` SQLite table.

- Ask the bot to list versions: *"show versions of my_script.py"*
- Restore a version: *"roll back my_script.py to version 3"*
- Or use `rollback_script` directly via the tool pipeline

---

## Script Run History

Every `run_script` execution and every cron run (via `cron_wrapper.py`) is logged to the `script_runs` table (exit code, stdout/stderr, duration).

- Ask the bot: *"show run history for my_script.py"*
- Or omit the name for a global view across all scripts

---

## Cron Failure Notifications

Scheduled scripts are invoked via `cron_wrapper.py` instead of directly. If a script exits with a non-zero code, the wrapper sends a Telegram alert to all allowed users with the script name and tail of its output.

---

## Destructive Command Confirmation

When the bot is about to run a command containing `rm`, `remove_cron`, `kill`, `pip uninstall`, `systemctl stop`, or similar — or when a drop-in tool declares a `confirm` function — it pauses and presents an inline **Yes / No** keyboard. The operation only proceeds on explicit confirmation.

---

## Security

- Only users listed in `TELEGRAM_ALLOWED_USERS` can interact with the bot
- The service runs as an unprivileged `tgbot` system user
- `.env` and `data/` are git-ignored

---

## License

MIT
