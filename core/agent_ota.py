"""OTA / AgentRunner 启用条件（服务端与客户端共用）。"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_SENTINEL_SKILLS = ("agent_runner", "ota")

_warned_sentinel_agents: set = set()


def _tools_ready(agent_def) -> bool:
    if agent_def is None:
        return False
    tools = getattr(agent_def, "tools", None)
    if not tools:
        return False
    return any(
        isinstance(t, dict) and t.get("enabled", True) is not False
        for t in tools
    )


def should_use_ota_agent(agent_def) -> bool:
    """是否走 AgentRunner / AgentBridge OTA。

    引擎选择以「执行后端」设置为准（UI 可见）：
    - backend == ``ota`` → OTA（工具列表非空且至少一个启用）
    - 其余后端 → 传统路径

    兼容：skills 里的旧哨兵（``ota`` / ``agent_runner``）在后端仍为默认
    ``nl_goal`` 时继续生效（deprecated —— 哨兵在技能下拉框里不存在，
    部署端不可见；建议改用执行后端设置）。
    """
    if agent_def is None or not _tools_ready(agent_def):
        return False
    backend = ""
    try:
        from app.runtime_config import (
            execution_backend_config,
            normalize_execution_backend,
        )

        backend = normalize_execution_backend(
            getattr(execution_backend_config, "backend", None)
        )
    except Exception:
        logger.debug("execution backend config unavailable for OTA dispatch", exc_info=True)
    if backend == "ota":
        return True
    if backend in ("", "nl_goal"):
        skills = getattr(agent_def, "skills", None) or []
        if any(s in skills for s in _SENTINEL_SKILLS):
            key = getattr(agent_def, "id", None) or getattr(agent_def, "name", None)
            if key not in _warned_sentinel_agents:
                _warned_sentinel_agents.add(key)
                logger.warning(
                    "OTA 由 skills 旧哨兵启用（agent=%s）——该开关 UI 不可见，"
                    "请改用「系统设置 → 执行后端 → 智能 OTA」并清掉 skills 中的 %s",
                    getattr(agent_def, "name", None),
                    "/".join(_SENTINEL_SKILLS),
                )
            return True
    return False
