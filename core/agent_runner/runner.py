# core/agent_runner/runner.py
"""True Agent OTA 循环引擎 — Observe → Think → Act。

AgentRunner 驱动基于 LLM 的自主浏览器代理，通过 OTA 循环：
1. Observe  — 采集当前页面状态（DOM 快照、URL、截图）
2. Think    — LLM 根据目标 + 上下文 + 观察决定下一步动作
3. Act      — 通过 Playwright MCP 执行动作并验证结果

循环直到目标达成或达到最大轮次。

与现有 step-by-step 循环的区别：
- step-by-step（_execution.py）：按预定义步骤逐条执行，LLM 只翻译单步
- OTA（本文件）：LLM 自主规划并执行，直到目标满足，适合开放性任务
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable

from core.agent_runner.context import AgentContext

logger = logging.getLogger(__name__)

# ── OTA Agent 系统提示词 ─────────────────────────────────────────────────────

OTA_SYSTEM_PROMPT = """You are a goal-driven browser automation agent using Playwright MCP.
Your task is to accomplish a user goal by observing the page and taking actions iteratively.

LOOP: Observe → Think → Act → Repeat until goal is met OR you declare done.

INPUT you will receive:
1. GOAL: The user's goal to accomplish
2. HISTORY: Previous actions and their results (from AgentContext)
3. CURRENT PAGE: Accessibility snapshot of the current browser page

ACTIONS available (maps to Playwright MCP tools):
- "click": Click an element by its ref. selector=ref, value=null.
- "fill": Type text into an input field by ref. selector=ref, value=text.
- "select": Select dropdown option by ref. selector=ref, value=option value/text.
- "goto": Navigate to a URL. selector=null, value=URL.
- "wait": Wait for text or time. selector=null, value=text or milliseconds.
- "screenshot": Take screenshot. selector=null, value=filename.
- "snapshot": Refresh page snapshot. selector=null, value=null.
- "done": Declare the goal is accomplished. selector=null, value=summary of what was achieved.
- "error": Report that the goal cannot be accomplished. selector=null, value=reason.

RULES:
- Use element refs from the snapshot (e.g., "e12") as selectors — do NOT invent CSS selectors.
- Think step by step about what needs to happen to achieve the goal.
- After each action, check if the goal is met. If yes, use action="done".
- If stuck after multiple attempts, use action="error" with explanation.
- Output ONLY the JSON object. No markdown fences, no explanation text.
- Always include "thinking" explaining your reasoning.
- For text input fields, match by role="textbox" and accessible name.
- For buttons, match by role="button" and accessible name.

OUTPUT SCHEMA (exact JSON):
{
  "action": "click",
  "selector": "e15",
  "value": null,
  "timeout_ms": 30000,
  "thinking": "To accomplish the goal I need to click the submit button...",
  "next_goal": "Verify login success"
}"""


# ── ToolRegistry ──────────────────────────────────────────────────────────────


@dataclass
class ToolDefinition:
    """工具定义，描述一个可执行的浏览器操作。"""
    name: str
    description: str
    action: str  # LLM 输出的 action 名称（click / fill / goto 等）


# 默认工具集（对应 LLM 可用的操作）
_DEFAULT_TOOLS: list[ToolDefinition] = [
    ToolDefinition("browser_click", "点击页面元素", "click"),
    ToolDefinition("browser_type", "在输入框中输入文本", "fill"),
    ToolDefinition("browser_select_option", "选择下拉选项", "select"),
    ToolDefinition("browser_navigate", "导航到指定 URL", "goto"),
    ToolDefinition("browser_wait_for", "等待文本出现或指定时间", "wait"),
    ToolDefinition("browser_take_screenshot", "截取页面截图", "screenshot"),
    ToolDefinition("browser_snapshot", "刷新页面可访问性快照", "snapshot"),
    ToolDefinition("browser_evaluate", "在浏览器中执行 JavaScript", "evaluate"),
]


class ToolRegistry:
    """工具注册表 — 将 LLM 输出的 action 名称映射到 MCP 工具调用。

    封装 PlaywrightMCPManager 的工具调用接口，提供：
    - execute(): 执行单个 LLM 工具调用
    - get_tool_definitions(): 获取可用工具列表（供 LLM prompt 使用）
    - get_actions_list(): 获取可用 action 名称列表

    使用方法::

        registry = ToolRegistry(mcp_manager)
        result = await registry.execute({"action": "click", "selector": "e15"})
    """

    def __init__(self, mcp_manager):
        """初始化工具注册表。

        Args:
            mcp_manager: PlaywrightMCPManager 实例（已启动）
        """
        self._mcp_manager = mcp_manager
        self._tools: dict[str, ToolDefinition] = {
            t.action: t for t in _DEFAULT_TOOLS
        }

    async def execute(self, tool_call: dict[str, Any]) -> dict[str, Any]:
        """执行单个工具调用。

        Args:
            tool_call: LLM 输出的工具调用字典，包含 action、selector、value 等字段

        Returns:
            {"success": bool, "error": str | None}
        """
        action = tool_call.get("action", "")

        # 特殊 action：done / error / snapshot 不需要 MCP 调用
        if action == "done":
            return {"success": True, "error": None, "done": True,
                    "summary": tool_call.get("value", "")}
        if action == "error":
            return {"success": False, "error": f"Agent 报告错误: {tool_call.get('value', '')}"}
        if action == "snapshot":
            return {"success": True, "error": None}

        # 标准 action → MCP 调用
        if action not in self._tools:
            return {"success": False, "error": f"未知操作: {action}"}

        try:
            result = await self._mcp_manager.execute_tool_call(tool_call)
            return result
        except Exception as exc:
            logger.exception("工具调用失败: %s", action)
            return {"success": False, "error": str(exc)}

    def get_tool_definitions(self) -> list[dict[str, str]]:
        """获取可用工具定义列表（供 LLM 上下文使用）。"""
        return [
            {"name": t.name, "description": t.description, "action": t.action}
            for t in _DEFAULT_TOOLS
        ]

    def get_actions_list(self) -> list[str]:
        """获取所有可用 action 名称列表。"""
        return [t.action for t in _DEFAULT_TOOLS]

    @property
    def mcp_manager(self):
        """获取底层 MCP 管理器（供高级操作使用）。"""
        return self._mcp_manager


# ── AgentRunner ───────────────────────────────────────────────────────────────


# 调试回调类型：async callable，接收 dict 参数
DebugCallback = Callable[[dict[str, Any]], Awaitable[None]]


class AgentRunner:
    """OTA 循环引擎：Observe → Think → Act。

    驱动基于 LLM 的自主浏览器代理，循环执行直到目标达成或失败。

    使用方法::

        runner = AgentRunner(
            mcp_manager=mcp,           # PlaywrightMCPManager 实例
            goal="登录系统并验证主页显示",  # 用户目标
            llm_client=client,         # AsyncOpenAI 客户端
            model="qwen-plus",         # LLM 模型
        )
        result = await runner.run()
        # {"status": "completed", "turns_used": 5, "result": {...}}
    """

    def __init__(
        self,
        mcp_manager,
        goal: str,
        llm_client,
        *,
        model: str | None = None,
        max_turns: int = 30,
        context_max_turns: int = 10,
        tool_timeout_ms: int = 30000,
        system_prompt: str | None = None,
        base_url: str | None = None,
    ):
        """初始化 AgentRunner。

        Args:
            mcp_manager: 已启动的 PlaywrightMCPManager 实例
            goal: 用户用自然语言描述的目标
            llm_client: AsyncOpenAI 客户端（通过 create_openai_client 创建）
            model: LLM 模型名（可选，覆盖全局配置）
            max_turns: OTA 循环最大轮次（默认 30）
            context_max_turns: AgentContext 窗口大小（默认 10）
            tool_timeout_ms: 单次工具调用超时（毫秒）
            system_prompt: 自定义系统提示词（默认使用 OTA_SYSTEM_PROMPT）
            base_url: 目标应用的基础 URL（用于导航步骤）
        """
        self._mcp_manager = mcp_manager
        self.goal = goal
        self._llm_client = llm_client
        self._model = model
        self.max_turns = max_turns
        self.tool_timeout_ms = tool_timeout_ms
        self.base_url = base_url

        self.tool_registry = ToolRegistry(mcp_manager)
        self.context = AgentContext(max_turns=context_max_turns)
        self._system_prompt = system_prompt or OTA_SYSTEM_PROMPT

        # 运行时状态
        self.current_url: str = ""
        self.turns_used: int = 0

    # ── OTA 三步 ─────────────────────────────────────────────────────────

    async def observe(self) -> dict[str, Any]:
        """采集当前页面状态。

        Returns:
            {"snapshot": str, "url": str, "screenshot_b64": str | None}
        """
        # 获取 DOM 快照
        snapshot = await self._mcp_manager.get_dom_snapshot()

        # 获取当前 URL
        url = ""
        try:
            result = await self._mcp_manager.call_tool("browser_evaluate", {
                "function": "window.location.href",
            })
            if result.get("success"):
                url = result.get("text", "")
        except Exception as exc:
            logger.debug("获取当前 URL 失败: %s", exc)

        self.current_url = url

        return {
            "snapshot": snapshot,
            "url": url,
            "screenshot_b64": None,  # 默认不截图，节省 token
        }

    async def think(self, observation: dict[str, Any]) -> dict[str, Any]:
        """LLM 根据目标 + 上下文 + 观察决定下一步动作。

        Args:
            observation: observe() 的返回值

        Returns:
            LLM 生成的工具调用字典（含 action, selector, value, thinking 等）
        """
        from core.llm_wrapper import generate_tool_call

        # 构建 LLM 输入
        history_text = self.context.get_context()
        snapshot = observation.get("snapshot", "(empty page)")

        step_description = (
            f"GOAL: {self.goal}\n\n"
            f"CURRENT URL: {observation.get('url', 'unknown')}\n\n"
            f"HISTORY:\n{history_text}\n\n"
            f"Based on the CURRENT PAGE snapshot below, decide the SINGLE NEXT ACTION "
            f"to move towards the GOAL. If the goal is already achieved, use action='done'."
        )
        if self._pending_hint:
            step_description += f"\n\nRETRY CONTEXT: {self._pending_hint}"
            self._pending_hint = None

        try:
            tool_call = await asyncio.wait_for(
                generate_tool_call(
                    step_description=step_description,
                    dom_snapshot=snapshot,
                    client=self._llm_client,
                    model=self._model,
                    system_prompt=self._system_prompt,
                    base_url=self.base_url,
                ),
                timeout=100,
            )
        except asyncio.TimeoutError:
            logger.warning("LLM think() 调用超时")
            return {
                "action": "error",
                "selector": None,
                "value": "LLM 调用超时",
                "thinking": "思考超时",
                "next_goal": None,
            }

        return tool_call.model_dump()

    async def act(self, action: dict[str, Any]) -> dict[str, Any]:
        """执行 LLM 决定的动作。

        Args:
            action: think() 返回的 LLM 工具调用字典

        Returns:
            {"success": bool, "error": str | None, "done": bool | None}
        """
        action_name = action.get("action", "?")
        logger.info("Act: %s (selector=%s, value=%s)",
                     action_name, action.get("selector"), action.get("value"))

        try:
            result = await asyncio.wait_for(
                self.tool_registry.execute(action),
                timeout=self.tool_timeout_ms / 1000.0,
            )
        except asyncio.TimeoutError:
            result = {
                "success": False,
                "error": f"操作超时 ({self.tool_timeout_ms}ms)",
            }

        return result

    # ── 主循环 ────────────────────────────────────────────────────────────

    async def run(
        self,
        debug_callback: DebugCallback | None = None,
    ) -> dict[str, Any]:
        """主 OTA 循环：observe → think → act 直到目标达成或达到最大轮次。

        Args:
            debug_callback: 可选的异步回调，每轮循环后调用，
                           接收 {"turn": int, "action": dict, "result": dict}

        Returns:
            {"status": "completed"|"failed"|"error",
             "turns_used": int,
             "result": str | None,
             "error": str | None,
             "context": dict | None}
        """
        start_time = time.monotonic()

        # 025-ref-click US3: 每轮运行重置恢复额度（stale 刷新 1 次 + heal 候选）
        self._retries_this_run = 0
        self._heal_exhausted = False
        self._pending_hint: str | None = None

        # 将目标写入上下文
        self.context.add_turn("user", f"目标: {self.goal}")

        for turn in range(1, self.max_turns + 1):
            self.turns_used = turn
            logger.info("━━━ Turn %d/%d ━━━", turn, self.max_turns)

            # ── Observe ──
            try:
                observation = await self.observe()
            except Exception as exc:
                logger.exception("observe() 异常 (turn %d)", turn)
                return self._make_result(
                    "error", turn, error=f"观察页面失败: {exc}",
                    start_time=start_time,
                )

            # ── Think ──
            try:
                action = await self.think(observation)
            except Exception as exc:
                logger.exception("think() 异常 (turn %d)", turn)
                return self._make_result(
                    "error", turn, error=f"LLM 思考失败: {exc}",
                    start_time=start_time,
                )

            thinking_text = action.get("thinking", "")
            logger.info("Thinking: %s", thinking_text[:120])

            # 检查 LLM 是否声明目标已达成
            if action.get("action") == "done":
                summary = action.get("value", "")
                logger.info("Agent 声明目标已达成: %s", summary)
                self.context.add_turn(
                    "assistant", f"目标已达成: {summary}",
                    tool_calls=[action],
                )
                return self._make_result(
                    "completed", turn,
                    result=summary,
                    start_time=start_time,
                )

            # ── Act ──
            try:
                result = await self.act(action)
            except Exception as exc:
                logger.exception("act() 异常 (turn %d)", turn)
                result = {"success": False, "error": str(exc)}

            # ── Act 失败恢复（025-ref-click US3）──
            if not result.get("success"):
                error = result.get("error", "") or ""
                from core.self_healing import (
                    build_failure_hint,
                    is_stale_ref_error,
                    recover_candidates,
                )

                if is_stale_ref_error(error) and self._retries_this_run < 1:
                    self._retries_this_run += 1
                    try:
                        fresh_obs = await self.observe()
                        self._pending_hint = build_failure_hint(error)
                        retry_action = await self.think(fresh_obs)
                        if isinstance(retry_action, dict) and retry_action.get("action") not in (
                            "error",
                            "done",
                        ):
                            action = retry_action
                            thinking_text = action.get("thinking", "") or thinking_text
                            result = await self.act(action)
                            logger.info(
                                "Stale-ref recovery %s (turn %d): success=%s",
                                self._retries_this_run, turn, result.get("success"),
                            )
                    except Exception as exc:
                        logger.warning("Stale-ref recovery failed (turn %d): %s", turn, exc)
                elif not self._heal_exhausted and not is_stale_ref_error(error):
                    heal_candidates = await recover_candidates(
                        self._mcp_manager,
                        step_description=self.goal,
                        error=error,
                    )
                    if heal_candidates:
                        try:
                            fresh_obs = await self.observe()
                            self._pending_hint = build_failure_hint(error, heal_candidates)
                            retry_action = await self.think(fresh_obs)
                            if isinstance(retry_action, dict) and retry_action.get("action") not in (
                                "error",
                                "done",
                            ):
                                action = retry_action
                                thinking_text = action.get("thinking", "") or thinking_text
                                result = await self.act(action)
                                logger.info(
                                    "Heal recovery (turn %d): success=%s",
                                    turn, result.get("success"),
                                )
                            else:
                                self._heal_exhausted = True
                        except Exception as exc:
                            logger.warning("Heal recovery failed (turn %d): %s", turn, exc)
                    else:
                        self._heal_exhausted = True

            # 记录到上下文
            action_summary = (
                f"{action.get('action', '?')}"
                + (f"({action.get('selector', '')})" if action.get('selector') else "")
                + (f" = {action.get('value', '')}" if action.get('value') else "")
            )
            self.context.add_turn(
                "assistant",
                thinking_text or action_summary,
                tool_calls=[action],
            )
            self.context.add_turn(
                "tool",
                "成功" if result.get("success") else f"失败: {result.get('error', '未知')}",
            )

            # 检查是否通过 act 的 done 标记完成了目标
            if result.get("done"):
                summary = result.get("summary", "")
                return self._make_result(
                    "completed", turn,
                    result=summary,
                    start_time=start_time,
                )

            # ── 调试回调 ──
            if debug_callback:
                try:
                    await debug_callback({
                        "turn": turn,
                        "action": action,
                        "result": result,
                        "context_tokens": self.context.estimate_tokens(),
                    })
                except Exception as exc:
                    logger.warning("debug_callback 异常: %s", exc)

            # 连续失败检测 — 3 次连续失败则提前终止
            if not result.get("success"):
                recent_failures = sum(
                    1 for t in self.context.turns[-6:]
                    if t.role == "tool" and "失败" in t.content
                )
                if recent_failures >= 3:
                    logger.warning("连续 %d 次失败，提前终止", recent_failures)
                    return self._make_result(
                        "failed", turn,
                        error="连续多次操作失败，可能目标无法达成",
                        start_time=start_time,
                    )

        # 达到最大轮次
        logger.warning("达到最大轮次 %d，目标未完成", self.max_turns)
        return self._make_result(
            "failed", self.max_turns,
            error=f"达到最大轮次 {self.max_turns}，目标未完成",
            start_time=start_time,
        )

    # ── 辅助方法 ───────────────────────────────────────────────────────────

    def _make_result(
        self,
        status: str,
        turns_used: int,
        *,
        result: str | None = None,
        error: str | None = None,
        start_time: float | None = None,
    ) -> dict[str, Any]:
        """构建统一的返回结果字典。"""
        duration_ms = 0.0
        if start_time is not None:
            duration_ms = (time.monotonic() - start_time) * 1000

        return {
            "status": status,
            "turns_used": turns_used,
            "result": result,
            "error": error,
            "duration_ms": round(duration_ms, 1),
            "context": self.context.to_dict() if status != "completed" else None,
        }

    def _is_goal_met(self, result: dict[str, Any]) -> bool:
        """检查目标是否已达成（简单启发式）。

        当前版本通过检查 ``done`` 标记或 LLM 输出判断。
        后续可接入 LLM 对当前页面状态的评估。
        """
        if result.get("done"):
            return True
        # 如果操作成功且 LLM 的 next_goal 为空或已达成，视为完成
        # （主要依赖 LLM 主动声明 done）
        return False

    @property
    def mcp_manager(self):
        """获取底层 MCP 管理器。"""
        return self._mcp_manager
