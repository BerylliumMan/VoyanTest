import asyncio
import json
import re
import logging

import httpx

logger = logging.getLogger(__name__)


def _strip_br(value: str) -> str:
    return re.sub(r"<\s*br\s*/?\s*>", " ", value, flags=re.IGNORECASE)


# AI 配置缓存：避免每次调用都查询 DB（特别是不跨事件循环兼容的场景）
_ai_config_cache: dict | None = None
_ai_config_lock = asyncio.Lock()


async def get_context_budget() -> int:
    """获取已配置的上下文窗口大小（tokens），用于请求分片预算。"""
    config = await _load_ai_config()
    return config.get('max_context_tokens', 131072)


def invalidate_ai_config_cache():
    """清除 AI 配置缓存，下次调用 call_model 时重新从 DB 加载。"""
    global _ai_config_cache
    _ai_config_cache = None


async def _load_ai_config(
    force_refresh: bool = False,
    agent_type: str | None = None,
    agent_id: int | None = None,
) -> dict:
    """Load AI config from DB（首次加载后缓存，避免跨事件循环访问 AsyncSessionLocal）。

    若指定 agent_type / agent_id，优先从 AgentDefinition 获取 overrides。
    """
    if agent_type or agent_id is not None:
        try:
            from app.agent_resolver import resolve_agent_config
            from app.database import AsyncSessionLocal
            async with AsyncSessionLocal() as db:
                agent_cfg = await resolve_agent_config(
                    db, agent_type or "generation", agent_id=agent_id,
                )
            # resolve_agent_config 已与全局 AIConfig 合并；无激活 Agent 时返回 None
            if agent_cfg is not None:
                logger.info(
                    "Using AgentDefinition llm_config agent_type=%s agent_id=%s model=%s",
                    agent_type, agent_id, agent_cfg.get("model") or "(global)",
                )
                return {
                    'model': agent_cfg.get('model', ''),
                    'api_key': agent_cfg.get('api_key', ''),
                    'api_base': agent_cfg.get('api_base', ''),
                    'temperature': agent_cfg.get('temperature', 0.1),
                    'max_context_tokens': agent_cfg.get('max_context_tokens', 131072),
                }
        except Exception:
            logger.debug(
                "AgentDefinition lookup failed type=%s id=%s, falling back to global",
                agent_type, agent_id,
            )

    global _ai_config_cache
    if _ai_config_cache and not force_refresh:
        return _ai_config_cache

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
        config = {
            'model': row.model,
            'api_key': decrypt_value(row.api_key),
            'api_base': row.api_base,
            'temperature': row.temperature,
            'max_context_tokens': row.max_context_tokens or 131072,
        }
    _ai_config_cache = config
    return config


async def call_model(
    messages: list,
    temperature: float | None = None,
    stream_callback=None,
    agent_type: str | None = None,
    agent_id: int | None = None,
) -> str:
    """Call the AI model using uitest-work's AI config / AgentDefinition overrides."""
    config = await _load_ai_config(agent_type=agent_type, agent_id=agent_id)

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
        "max_tokens": min(config.get('max_context_tokens', 131072) // 3, 16384),
    }

    if stream_callback:
        payload["stream"] = True
        full_content = []
        async with httpx.AsyncClient(timeout=600) as client:
            async with client.stream("POST", api_url, json=payload, headers=headers) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
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
        async with httpx.AsyncClient(timeout=600) as client:
            resp = await client.post(api_url, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            choice = data["choices"][0]
            finish_reason = choice.get("finish_reason", "unknown")
            raw = choice["message"]["content"]
            if finish_reason == "length":
                logger.warning("Model output truncated (finish_reason=length), content length: %d chars", len(raw))
            return _strip_br(raw)
