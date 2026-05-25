#!/usr/bin/env python3
"""Send a Telegram message to all allowed users. Importable or CLI."""
import json
import os
import sys
import urllib.request
from pathlib import Path

# Load .env when used as CLI or imported outside cron_wrapper
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass

_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
_USERS = [
    int(x.strip())
    for x in os.environ.get("TELEGRAM_ALLOWED_USERS", "").split(",")
    if x.strip().isdigit()
]


def send(text: str, parse_mode: str = "HTML") -> None:
    """Send text to all allowed Telegram users. Prints errors to stderr."""
    if not _TOKEN or not _USERS:
        print("[notify] TELEGRAM_BOT_TOKEN or TELEGRAM_ALLOWED_USERS not set", file=sys.stderr)
        return
    for user_id in _USERS:
        payload = json.dumps({"chat_id": user_id, "text": text, "parse_mode": parse_mode}).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{_TOKEN}/sendMessage",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        try:
            urllib.request.urlopen(req, timeout=15)
        except Exception as e:
            print(f"[notify] send to {user_id} failed: {e}", file=sys.stderr)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: notify.py <message>", file=sys.stderr)
        sys.exit(1)
    send(sys.argv[1])
