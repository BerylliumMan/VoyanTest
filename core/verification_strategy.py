# core/verification_strategy.py
"""
验证策略规则引擎。

根据操作类型决定是否执行验证、验证范围以及验证严格程度。
纯规则引擎，不依赖 AI / LLM，仅基于操作类型做确定性判断。

设计原则：
- 导航/点击类操作：始终验证，全页范围，最高严格度（页面结构变化风险高）
- 输入/选择类操作：仅 MCP 错误时验证，局部范围，中等严格度（值写入 DOM 即确认）
- 交互类操作：始终验证，元素范围，最低严格度（hover/drag 影响可控）
- 未知操作：默认严格验证（安全策略，宁可多验证不可漏检）
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 操作分类映射
# ---------------------------------------------------------------------------

# 导航/点击 — 始终验证，全页，level 2
_NAVIGATE_ACTIONS: frozenset[str] = frozenset({"click", "goto"})

# 输入/选择 — 仅 mcp_error 时验证，局部，level 1
_INPUT_ACTIONS: frozenset[str] = frozenset({"fill", "select"})

# 交互 — 始终验证，元素级，level 0
_INTERACTIVE_ACTIONS: frozenset[str] = frozenset({"hover", "drag"})

_SCOPE_MAP: dict[str, str] = {
    "click": "full_page",
    "goto": "full_page",
    "fill": "fill_value",
    "select": "fill_value",
    "hover": "element_only",
    "drag": "element_only",
}

_LEVEL_MAP: dict[str, int] = {
    "click": 2,
    "goto": 2,
    "fill": 1,
    "select": 1,
    "hover": 0,
    "drag": 0,
}


class VerificationStrategy:
    """确定操作是否需要验证以及如何验证的规则引擎。

    全部为静态方法，无状态，可安全同时调用。
    """

    # ------------------------------------------------------------------
    # should_verify — 信号驱动的验证决策
    # ------------------------------------------------------------------

    @staticmethod
    def should_verify(action: str, mcp_error: str | None = None) -> bool:
        """根据操作类型和 MCP 执行结果决定是否需要验证。

        Args:
            action: 操作类型（click / goto / fill / select / hover / drag 等）
            mcp_error: MCP 执行器的错误信息，None 表示执行成功

        Returns:
            True 表示需要执行验证
        """
        # 导航/点击/交互类 — 始终验证
        if action in _NAVIGATE_ACTIONS:
            return True
        if action in _INTERACTIVE_ACTIONS:
            return True

        # 输入/选择类 — 仅 MCP 错误时验证（成功时值已写入 DOM，无需额外验证）
        if action in _INPUT_ACTIONS:
            return bool(mcp_error)

        # 未知操作 — 保守策略：默认验证
        logger.debug("未知操作类型 %r，采用保守策略：默认验证", action)
        return True

    # ------------------------------------------------------------------
    # get_verification_scope — 确定验证范围
    # ------------------------------------------------------------------

    @staticmethod
    def get_verification_scope(action: str) -> str:
        """返回操作的验证范围。

        Returns:
            "full_page" / "fill_value" / "element_only"
        """
        scope = _SCOPE_MAP.get(action, "full_page")
        if scope == "full_page" and action not in _SCOPE_MAP:
            logger.debug("未知操作类型 %r，默认全页验证", action)
        return scope

    # ------------------------------------------------------------------
    # get_verification_level — 确定验证严格程度
    # ------------------------------------------------------------------

    @staticmethod
    def get_verification_level(action: str) -> int:
        """返回操作的验证严格等级（0~2）。

        - 0: 轻量验证（hover / drag）
        - 1: 中等验证（fill / select）
        - 2: 严格验证（click / goto / unknown）

        Returns:
            0, 1, 或 2
        """
        level = _LEVEL_MAP.get(action, 2)
        if level == 2 and action not in _LEVEL_MAP:
            logger.debug("未知操作类型 %r，默认严格验证 level=2", action)
        return level


# ---------------------------------------------------------------------------
# 模块级单例（兼容调用方直接 import 使用）
# ---------------------------------------------------------------------------

VERIFICATION_STRATEGY = VerificationStrategy()
