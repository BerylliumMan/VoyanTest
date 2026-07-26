"""OTA / AgentRunner 启用条件（服务端与客户端共用）。"""

from __future__ import annotations


def should_use_ota_agent(agent_def) -> bool:
    """是否走 AgentRunner / AgentBridge OTA。

    须同时满足：
    - skills 显式包含 ``agent_runner`` 或 ``ota``（避免仅配置 tools 就绕过用例步骤）
    - tools 非空且至少有一个 enabled 不为 False 的工具
    """
    if agent_def is None:
        return False
    skills = getattr(agent_def, "skills", None) or []
    if not any(s in skills for s in ("agent_runner", "ota")):
        return False
    tools = getattr(agent_def, "tools", None)
    if not tools:
        return False
    return any(
        isinstance(t, dict) and t.get("enabled", True) is not False
        for t in tools
    )
