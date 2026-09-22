"""OTA / AgentRunner 启用条件（服务端与客户端共用）。"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


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

    UI 执行引擎仅保留 OTA：工具列表非空且至少一个启用时返回 True。
    """
    return _tools_ready(agent_def)
