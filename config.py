from dotenv import load_dotenv
import os

load_dotenv()

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_ALLOWED_USERS = [
    int(x.strip()) for x in os.environ["TELEGRAM_ALLOWED_USERS"].split(",")
]

LLM_BASE_URL = os.getenv("LLM_BASE_URL", "http://localhost:8080/v1")
LLM_MODEL = os.getenv("LLM_MODEL", "local")
LLM_MAX_TOKENS = int(os.getenv("LLM_MAX_TOKENS", "512"))
LLM_TEMPERATURE = float(os.getenv("LLM_TEMPERATURE", "0.7"))

CONTEXT_LIMIT = int(os.getenv("CONTEXT_LIMIT", "10240"))
COMPACTION_THRESHOLD = float(os.getenv("COMPACTION_THRESHOLD", "0.65"))
WARN_THRESHOLD = float(os.getenv("WARN_THRESHOLD", "0.50"))
HISTORY_WINDOW = int(os.getenv("HISTORY_WINDOW", "8"))

SEARXNG_URL = os.getenv("SEARXNG_URL", "")
TIMEZONE = os.getenv("TIMEZONE", "Europe/London")
