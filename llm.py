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
                    logger.debug(f"LLM structured raw response: {content}")
                    try:
                        return json.loads(content)
                    except json.JSONDecodeError as json_err:
                        raise ValueError(
                            f"LLM returned invalid JSON: {json_err}\nRaw content: {content!r}"
                        ) from json_err
        except Exception as e:
            if attempt == 0:
                logger.warning(f"LLM structured call failed (attempt 1): {type(e).__name__}: {e}, retrying...")
                continue
            logger.error(f"LLM structured call failed: {type(e).__name__}: {e}")
            raise

async def llm_stream(messages: list, max_tokens: int = 512):
    """Stream plain text response from llama-server via SSE. Yields text chunks."""
    payload = {
        "model": config.LLM_MODEL,
        "messages": messages,
        "temperature": config.LLM_TEMPERATURE,
        "max_tokens": max_tokens,
        "stream": True,
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{config.LLM_BASE_URL}/chat/completions",
                json=payload,
                headers={"Authorization": "Bearer local"},
                timeout=aiohttp.ClientTimeout(total=120)
            ) as resp:
                resp.raise_for_status()
                async for raw_line in resp.content:
                    line = raw_line.decode("utf-8").strip()
                    if not line.startswith("data: "):
                        continue
                    data_str = line[6:]
                    if data_str == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data_str)
                        delta = chunk["choices"][0]["delta"].get("content", "")
                        if delta:
                            yield delta
                    except (json.JSONDecodeError, KeyError, IndexError):
                        continue
    except Exception as e:
        logger.error(f"LLM stream failed: {e}")
        yield f"[LLM error: {e}]"


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
