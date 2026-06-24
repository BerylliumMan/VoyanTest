import json
import re
import requests
import logging

logger = logging.getLogger(__name__)


def _strip_br(value: str) -> str:
    return re.sub(r"<\s*br\s*/?\s*>", " ", value, flags=re.IGNORECASE)


async def _load_ai_config() -> dict:
    """Load AI config from uitest-work's ai_configs table."""
    from app.database import AsyncSessionLocal
    from app import db_models
    from app.security.encryption import decrypt_value
    from sqlalchemy import select

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(db_models.AIConfig).where(db_models.AIConfig.id == 1)
        )
        row = result.scalar_one_or_none()
        if not row:
            raise RuntimeError(
                "AI config not found. Configure via Settings page."
            )
        return {
            'model': row.model,
            'api_key': decrypt_value(row.api_key),
            'api_base': row.api_base,
            'temperature': row.temperature,
        }


async def call_model(messages: list, temperature: float | None = None, stream_callback=None) -> str:
    """Call the AI model using uitest-work's AI config."""
    config = await _load_ai_config()

    api_url = config['api_base'].rstrip('/')
    # Ensure URL includes /chat/completions path (some proxies like OneAPI store base URL without it)
    if not api_url.endswith('/chat/completions'):
        api_url += '/chat/completions'
    model_name = config['model']
    api_key = config['api_key']

    if not model_name:
        raise RuntimeError("MODEL_NAME not configured.")

    if temperature is None:
        temperature = config.get('temperature', 0.1)

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    payload = {
        "model": model_name,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": 8192,
    }

    if stream_callback:
        payload["stream"] = True
        full_content = []
        with requests.post(api_url, json=payload, headers=headers, timeout=600, stream=True) as resp:
            resp.raise_for_status()
            for line in resp.iter_lines(decode_unicode=True):
                if not line:
                    continue
                if line.startswith('data: '):
                    data_str = line[6:]
                    if data_str == '[DONE]':
                        break
                    try:
                        data = json.loads(data_str)
                        chunk = data.get('choices', [{}])[0].get('delta', {}).get('content', '')
                        if chunk:
                            full_content.append(chunk)
                            stream_callback(chunk)
                    except json.JSONDecodeError:
                        continue
        return _strip_br(''.join(full_content))
    else:
        resp = requests.post(api_url, json=payload, headers=headers, timeout=600)
        resp.raise_for_status()
        data = resp.json()
        choice = data["choices"][0]
        finish_reason = choice.get("finish_reason", "unknown")
        raw = choice["message"]["content"]
        if finish_reason == "length":
            logger.warning("Model output truncated (finish_reason=length), content length: %d chars", len(raw))
        return _strip_br(raw)
