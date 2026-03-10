# Materia

> *Small spells. Real magic.*

Materia is a local-first Telegram bot running on a Proxmox server. It routes natural-language messages through an intent classifier to a suite of built-in tools, with the ability to create and hot-reload new tools on demand.

---

## Features

- **Intent classification** via a local LLM (llama-server / OpenAI-compatible)
- **12 built-in tools**: chat, web search, Hacker News briefing, script creation, cron management, tool creation, memory
- **Persistent memory** stored in SQLite (`data/memory.db`)
- **Context compaction** — summarises conversation history when the context window fills up
- **Hot-reload** of user-created tools without restarting the bot
- **User allowlist** — only configured Telegram user IDs can interact

---

## Requirements

- Python 3.11+
- A running llama-server instance (OpenAI-compatible `/v1` endpoint)
- A Telegram bot token (from [@BotFather](https://t.me/botfather))
- Optional: SearXNG instance for web search

---

## Directory Structure

```
/opt/tgbot/
├── bot.py              # Entry point, Telegram dispatcher
├── intent.py           # LLM-based intent classifier
├── router.py           # Maps intents to tool handlers
├── memory.py           # SQLite persistence (memory, conversations, sessions)
├── context.py          # Token counting and context compaction
├── llm.py              # LLM client (structured + plain text)
├── config.py           # Environment variable loading
├── tools/
│   ├── __init__.py
│   ├── builtin.py      # All 12 built-in tools
│   └── user_tools.py   # Hot-reloaded user-created tools
├── manifest.json       # Tool registry
├── scripts/            # User-generated Python scripts
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
| `/context` | Show token usage breakdown |
| `/compact` | Force context compaction now |
| `/tools` | List all registered tools |
| `/scripts` | List scripts in `/opt/tgbot/scripts/` |

---

## Built-in Tools

| Tool | Description |
|---|---|
| `chat` | General conversation and questions |
| `web_search` | Search via SearXNG (falls back to DuckDuckGo) |
| `hn_briefing` | Hacker News top stories with optional topic filter |
| `create_script` | Generate a Python script and optionally schedule it |
| `list_scripts` | List all scripts with their cron schedules |
| `run_script` | Manually run a script by name |
| `add_cron` | Add or modify a cron entry for a script |
| `remove_cron` | Remove a cron entry for a script |
| `create_tool` | Create a new tool from a plain-English description |
| `list_tools` | List all available tools |
| `memory_set` | Save a preference or fact (`key: value`) |
| `memory_get` | Retrieve a stored preference or fact |

---

## Context Compaction

When conversation history reaches `COMPACTION_THRESHOLD` (65% of `CONTEXT_LIMIT` by default), the bot:

1. Sends the full history to the LLM for summarisation
2. Appends the summary to `MEMORY.md`
3. Extracts structured key-value facts into the SQLite `memory` table
4. Clears the conversation history, keeping only the last 2 messages

Force compaction manually with `/compact`.

---

## Creating Custom Tools

Send a message like:

> "Create a tool that checks the current Bitcoin price and returns it in GBP"

The bot will:
1. Generate an async Python function via the LLM
2. Append it to `tools/user_tools.py`
3. Register it in `manifest.json`
4. Hot-reload the module — no restart needed

---

## Security

- Only users listed in `TELEGRAM_ALLOWED_USERS` can interact with the bot
- The service runs as an unprivileged `tgbot` system user
- `.env` and `data/` are git-ignored

---

## License

MIT
