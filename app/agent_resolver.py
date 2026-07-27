# app/agent_resolver.py — Agent 配置 TTL 内存缓存
#
# 为 LLM 调用热路径（gen/exec/recording）提供零 DB 查询的 Agent 配置查找。
# 缓存每 30 秒过期一次；CRUD 操作通过 invalidate_agent_cache 主动失效。
import logging
import time
from typing import Dict, Optional, Tuple

from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# 缓存结构: {cache_key: (cached_at_timestamp, llm_config_dict_or_None)}
# cache_key 为 agent_type，或 "id:{agent_id}"。
# llm_config 为 None 表示该类型当前无激活 Agent，同样缓存以避免重复 DB 查询。
_config_cache: Dict[str, Tuple[float, Optional[dict]]] = {}

# 全局 AIConfig 缓存（避免每次 merge 都查库）
_global_config_cache: Optional[dict] = None
_global_config_cached_at: float = 0
_GLOBAL_TTL = 30

TTL_SECONDS = 30


def merge_llm_config(defaults: dict, overrides: dict | None) -> dict:
    """Merge Agent llm_config onto global defaults; skip empty overrides.

    Empty / whitespace-only strings (e.g. unset model) do not replace defaults,
    so Agents can leave model blank and inherit AIConfig.model.
    """
    merged = dict(defaults or {})
    for key, value in (overrides or {}).items():
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        merged[key] = value
    return merged


async def _load_global_ai_config() -> dict:
    """加载系统全局 AI 配置（带缓存），用作 Agent 配置的默认值。"""
    global _global_config_cache, _global_config_cached_at
    now = time.time()
    if _global_config_cache is not None and now - _global_config_cached_at < _GLOBAL_TTL:
        return _global_config_cache

    from app.database import AsyncSessionLocal
    from sqlalchemy import select
    from app import db_models
    from app.security.encryption import decrypt_value

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(db_models.AIConfig).where(db_models.AIConfig.id == 1)
        )
        row = result.scalar_one_or_none()
        if not row:
            _global_config_cache = {}
            _global_config_cached_at = now
            return {}

        config = {
            'model': row.model or '',
            'api_key': decrypt_value(row.api_key) if row.api_key else '',
            'api_base': row.api_base or '',
            'temperature': row.temperature or 0.1,
            'max_context_tokens': row.max_context_tokens or 131072,
        }
    _global_config_cache = config
    _global_config_cached_at = now
    return config


async def resolve_agent_config(
    db: AsyncSession,
    agent_type: str,
    agent_id: int | None = None,
) -> Optional[dict]:
    """获取 Agent 的 llm_config（按 agent_id 或该类型当前激活项）。

    返回的配置以系统全局 AIConfig 为默认值，Agent 的 llm_config 逐个字段覆盖。
    例如：Agent 只设了 model，则 api_key/api_base 继承自全局配置。

    优先从内存缓存返回；缓存过期或未命中时查询数据库并回填缓存。
    返回 None 表示未找到可用的 AgentDefinition。
    """
    now = time.time()
    cache_key = f"id:{agent_id}" if agent_id is not None else agent_type

    # 缓存命中：在 TTL 窗口内直接返回
    if cache_key in _config_cache:
        cached_at, cached_config = _config_cache[cache_key]
        if now - cached_at < TTL_SECONDS:
            return cached_config

    # 缓存未命中或已过期：查询数据库
    from app.crud.agent_definition import get_active_by_type, get_agent_definition

    active_agent = None
    if agent_id is not None:
        active_agent = await get_agent_definition(db, agent_id)
    if active_agent is None:
        active_agent = await get_active_by_type(db, agent_type)

    if active_agent is not None:
        defaults = await _load_global_ai_config()
        agent_cfg: dict = active_agent.llm_config or {}
        llm_config = merge_llm_config(defaults, agent_cfg)
        logger.debug(
            "Agent 配置缓存回填: key=%s id=%s model=%s",
            cache_key, active_agent.id, llm_config.get("model", "?"),
        )
    else:
        llm_config = None
        logger.debug("Agent 配置缓存回填: key=%s → 无可用 Agent", cache_key)

    _config_cache[cache_key] = (now, llm_config)
    return llm_config


def invalidate_agent_cache(agent_type: Optional[str] = None) -> None:
    """显式失效 Agent 配置缓存。

    Args:
        agent_type: 指定要失效的类型；为 None 时清空全部缓存。
                    传入类型时同时清除该类型与所有 id: 缓存项。
    """
    if agent_type is not None:
        if agent_type in _config_cache:
            del _config_cache[agent_type]
        # Also drop per-id entries (safe; next resolve will refill)
        for key in [k for k in _config_cache if k.startswith("id:")]:
            del _config_cache[key]
        logger.debug("Agent 缓存已失效: agent_type=%s (+id keys)", agent_type)
    else:
        _config_cache.clear()
        logger.debug("Agent 缓存已全部清空")
