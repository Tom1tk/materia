import aiohttp
import json
import logging
import config

logger = logging.getLogger(__name__)

async def llm_structured(messages: list, schema: dict) -> dict:
    """Call llama-server with JSON schema response format. Returns parsed dict."""
    payload = {
        "model": config.LLM_MODEL,
        "messages": messages,
        "temperature": config.LLM_TEMPERATURE,
        "max_tokens": config.LLM_MAX_TOKENS,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "response",
                "schema": schema,
                "strict": True
            }
        }
    }
    for attempt in range(2):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{config.LLM_BASE_URL}/chat/completions",
                    json=payload,
                    headers={"Authorization": f"Bearer local"},
                    timeout=aiohttp.ClientTimeout(total=30)
                ) as resp:
                    resp.raise_for_status()
                    data = await resp.json()
                    content = data["choices"][0]["message"]["content"]
                    return json.loads(content)
        except Exception as e:
            if attempt == 0:
                logger.warning(f"LLM structured call failed (attempt 1): {e}, retrying...")
                continue
            logger.error(f"LLM structured call failed: {e}")
            raise

async def llm_plain(messages: list, max_tokens: int = 512) -> str:
    """Call llama-server for plain text response. Returns string."""
    payload = {
        "model": config.LLM_MODEL,
        "messages": messages,
        "temperature": config.LLM_TEMPERATURE,
        "max_tokens": max_tokens,
    }
    for attempt in range(2):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{config.LLM_BASE_URL}/chat/completions",
                    json=payload,
                    headers={"Authorization": "Bearer local"},
                    timeout=aiohttp.ClientTimeout(total=30)
                ) as resp:
                    resp.raise_for_status()
                    data = await resp.json()
                    content = data["choices"][0]["message"]["content"]
                    return content or ""
        except Exception as e:
            if attempt == 0:
                logger.warning(f"LLM plain call failed (attempt 1): {e}, retrying...")
                continue
            logger.error(f"LLM plain call failed: {e}")
            return f"[LLM error: {e}]"
