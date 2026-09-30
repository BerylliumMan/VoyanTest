# core/agent_runner/runner.py
"""True Agent OTA 循环引擎 — Observe → Think → Act。

AgentRunner 驱动基于 LLM 的自主浏览器代理，通过 OTA 循环：
1. Observe  — 采集当前页面状态（DOM 快照、URL、按需截图）
2. Think    — LLM 根据目标 + 上下文 + 观察决定下一步动作
3. Act      — 通过 Playwright MCP 执行动作并验证结果

循环直到目标达成或达到最大轮次。

与 Cursor IDE 浏览器工具对齐：快照 ref 优先；困难页/失败轮带截图；
ref 点击失败时 bbox→click_xy 兜底。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Awaitable

from core.agent_runner.context import AgentContext
from core.mcp_args import ACTION_DESCRIPTIONS, resolve_mcp_tool
from core.ota_cursor import (
    OTA_CURSOR_SYSTEM_PROMPT,
    capture_screenshot_b64,
    click_xy_fallback_after_ref_fail,
    maybe_rewrite_click_xy_from_ref,
    should_capture_screenshot,
)
from app.crud import agent_run as crud_agent_run

logger = logging.getLogger(__name__)

# Backward-compatible alias
OTA_SYSTEM_PROMPT = OTA_CURSOR_SYSTEM_PROMPT


# ── ToolRegistry ──────────────────────────────────────────────────────────────


@dataclass
class ToolDefinition:
    """工具定义，描述一个可执行的浏览器操作。"""
    name: str
    description: str
    action: str  # LLM 输出的 action 名称（click / fill / goto 等）


_RUNNER_ACTIONS = (
    "click",
    "double_click",
    "right_click",
    "fill",
    "select",
    "goto",
    "wait",
    "screenshot",
    "snapshot",
    "evaluate",
    "press_key",
    "hover",
    "drag",
    "scroll",
    "check",
    "dialog",
    "upload",
    "drop",
    "tabs",
    "navigate_back",
    "click_xy",
    "move_mouse",
    "drag_xy",
)

_DEFAULT_TOOLS: list[ToolDefinition] = [
    ToolDefinition(
        resolve_mcp_tool(action), ACTION_DESCRIPTIONS.get(action, action), action
    )
    for action in _RUNNER_ACTIONS
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
                    "summary": tool_call.get("value") or ""}
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
        case_steps: list[dict[str, Any]] | None = None,
        base_url: str | None = None,
        db=None,
        run_id: int | None = None,
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
            db: 可选的 AsyncSession，与 run_id 一起启用每轮 OTA 消息持久化
            run_id: 关联的 agent_runs 记录 ID（见 db）
        """
        self._mcp_manager = mcp_manager
        self.goal = goal
        self._llm_client = llm_client
        self._model = model
        self.max_turns = max_turns
        self.tool_timeout_ms = tool_timeout_ms
        self.base_url = base_url

        self._db = db
        self._run_id = run_id

        self.tool_registry = ToolRegistry(mcp_manager)
        self.context = AgentContext(max_turns=context_max_turns)
        self._system_prompt = system_prompt or OTA_SYSTEM_PROMPT
        # Phase B(auto)：清单步骤 + 覆盖集合（确定性自动成组用）
        self._run_steps: list[dict[str, Any]] = list(case_steps or [])
        self._journal_covered: set[int] = set()
        self._auto_unit_off = False

        # 运行时状态
        self.current_url: str = ""
        self.turns_used: int = 0
        self._force_next_screenshot: bool = False
        self._consecutive_act_failures: int = 0
        # 成功动作日志（供 run 结束后合成固化脚本；见 _collect_journal_entry）
        self.journal: list[dict[str, Any]] = []
        self._last_candidates: tuple = ()
        self._pending_hint: str | None = None

    # ── OTA 三步 ─────────────────────────────────────────────────────────

    async def observe(
        self,
        *,
        force_screenshot: bool = False,
    ) -> dict[str, Any]:
        """采集当前页面状态（快照；困难页/失败轮按需截图）。

        Returns:
            {"snapshot": str, "url": str, "screenshot_b64": str | None, ...}
        """
        snapshot = await self._mcp_manager.get_dom_snapshot()
        from core.locator_candidates import (
            actionable_candidates,
            extract_candidates,
            snapshot_version,
        )

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
        candidates = actionable_candidates(extract_candidates(snapshot or ""))
        self._last_candidates = candidates

        want_shot = should_capture_screenshot(
            snapshot,
            consecutive_failures=self._consecutive_act_failures,
            force=force_screenshot or self._force_next_screenshot,
            pending_hint=self._pending_hint,
        )
        self._force_next_screenshot = False
        screenshot_b64 = None
        if want_shot:
            screenshot_b64 = await capture_screenshot_b64(self._mcp_manager)

        return {
            "snapshot": snapshot,
            "url": url,
            "screenshot_b64": screenshot_b64,
            "snapshot_version": snapshot_version(snapshot or ""),
            "candidates": candidates,
        }

    async def think(self, observation: dict[str, Any]) -> dict[str, Any]:
        """LLM 根据目标 + 上下文 + 观察（含可选截图）决定下一步动作。"""
        from core.llm_wrapper import generate_tool_call

        history_text = self.context.get_context()
        snapshot = observation.get("snapshot", "(empty page)")

        step_description = (
            f"GOAL: {self.goal}\n\n"
            f"CURRENT URL: {observation.get('url', 'unknown')}\n\n"
            f"HISTORY:\n{history_text}\n\n"
            f"Based on the CURRENT PAGE snapshot below, decide the NEXT ACTION — either one action, "
            f"or a 'unit' array of up to 6 MECHANICAL sub-actions (fill/click/select/"
            f"press_key/wait/assert_text with optional fallbacks) — "
            f"to move towards the GOAL. If the goal is already achieved, use action='done'."
        )
        from core.locator_candidates import serialize_candidates
        step_description += (
            f"\n\nCANDIDATE ELEMENTS (choose selector only from this list):\n"
            f"{serialize_candidates(observation.get('candidates', ())) or '(none)'}"
        )
        if observation.get("screenshot_b64"):
            step_description += (
                "\n\nA VIEWPORT SCREENSHOT is attached for this turn. "
                "Prefer snapshot refs first; use click_xy from the image when refs are unreliable."
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
                    screenshot_b64=observation.get("screenshot_b64"),
                    replace_system_prompt=True,
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

        action = tool_call.model_dump()
        from core.locator_candidates import is_snapshot_ref, validate_candidate_ref
        selector = action.get("selector")
        act_name = (action.get("action") or "").strip().lower()
        # click_xy may carry a ref in selector — rewrite later; skip ref whitelist for coords
        if is_snapshot_ref(selector) and act_name not in (
            "click_xy", "browser_mouse_click_xy", "move_mouse", "drag_xy",
        ):
            validation = validate_candidate_ref(
                selector,
                snapshot_version=observation.get("snapshot_version", ""),
                decision_version=observation.get("snapshot_version", ""),
                candidates=observation.get("candidates", ()),
            )
            if not validation.valid:
                return {
                    "action": "error",
                    "selector": None,
                    "value": f"locator rejected: {validation.failure_kind}",
                    "thinking": "候选元素校验失败",
                }
        action["snapshot_version"] = observation.get("snapshot_version")
        action = await maybe_rewrite_click_xy_from_ref(
            action,
            mcp_manager=self._mcp_manager,
            candidates=observation.get("candidates") or self._last_candidates,
        )
        return action

    async def act(self, action: dict[str, Any]) -> dict[str, Any]:
        """执行 LLM 决定的动作（含瞬时失败本地重试，不计失败轮）。"""
        action_name = action.get("action", "?")
        logger.info("Act: %s (selector=%s, value=%s)",
                     action_name, action.get("selector"), action.get("value"))

        result = await self._execute_action_once(action)
        if not result.get("success"):
            from core.mcp_args import is_transient_action_error

            if is_transient_action_error(result.get("error")):
                for _delay in (0.6, 1.5):
                    logger.info(
                        "Runner: act 瞬时失败，本地重试（%.1fs 后）: %s",
                        _delay, str(result.get("error"))[:80],
                    )
                    await asyncio.sleep(_delay)
                    result = await self._execute_action_once(action)
                    if result.get("success"):
                        break
        return result

    async def _execute_action_once(self, action: dict[str, Any]) -> dict[str, Any]:
        try:
            return await asyncio.wait_for(
                self.tool_registry.execute(action),
                timeout=self.tool_timeout_ms / 1000.0,
            )
        except asyncio.TimeoutError:
            return {
                "success": False,
                "error": f"操作超时 ({self.tool_timeout_ms}ms)",
            }

    def _auto_unit_from_config_disabled(self) -> bool:
        try:
            from app.config import get_settings

            cfg = get_settings()
            return not (
                getattr(cfg, "action_unit_enabled", True)
                and getattr(cfg, "action_unit_auto", True)
            )
        except Exception:
            return False

    def _auto_unit_max(self) -> int:
        try:
            from app.config import get_settings

            return int(getattr(get_settings(), "action_unit_max", 6) or 6)
        except Exception:
            return 6

    async def _execute_action_unit(
        self, turn: int, unit_raw: Any, observation: dict[str, Any]
    ) -> dict[str, Any]:
        """Phase B：宏动作单元（服务端 MCP 版）——多子动作、逐步入账、失败原地停止。"""
        from app.config import get_settings

        s = get_settings()
        if not getattr(s, "action_unit_enabled", True):
            return {"success": False, "disabled": True}
        max_steps = int(getattr(s, "action_unit_max", 6) or 6)

        from core.locator_candidates import is_snapshot_ref

        _cands = observation.get("candidates") or []
        _refs = {getattr(c, "ref", None) for c in _cands}

        def _valid_ref(sel: str) -> bool:
            if not is_snapshot_ref(sel or ""):
                return True
            return sel in _refs

        from core.action_unit import normalize_unit, run_unit

        steps, problems = normalize_unit(
            unit_raw, max_steps=max_steps, is_valid_ref=_valid_ref
        )
        if problems:
            logger.info("unit: 规范化丢弃 %s（turn %d）", problems, turn)
        if not steps:
            self.context.add_turn(
                "assistant",
                "SYSTEM: unit 没有可执行子动作（" + "; ".join(problems[:3]) + "），请改用单动作。",
            )
            return {"success": False, "invalid": True}

        async def _exec(sub_action: dict[str, Any]) -> dict[str, Any]:
            return await self._execute_action_once(sub_action)

        def _on_ok(sub_action: dict[str, Any], _result: dict[str, Any]) -> None:
            try:
                self._collect_journal_entry(sub_action, turn)
            except Exception:
                logger.debug("unit journal 记录跳过", exc_info=True)

        outcome = await run_unit(steps, _exec, on_step_ok=_on_ok)
        if outcome.success:
            logger.info("unit: %d 步执行成功（turn %d）", outcome.executed_count, turn)
            self.context.add_turn(
                "assistant", f"宏动作单元：{outcome.executed_count} 步一次往返执行成功",
                tool_calls=list(outcome.executed),
            )
            return {"success": True, "executed": outcome.executed_count}
        logger.info(
            "unit: 第 %d 步失败停止（已完成 %d 步）: %s（turn %d）",
            (outcome.failed_index or 0) + 1, outcome.executed_count,
            str(outcome.error)[:100], turn,
        )
        self.context.add_turn(
            "assistant",
            f"SYSTEM: 宏动作单元在第 {(outcome.failed_index or 0) + 1} 步失败"
            f"（已完成 {outcome.executed_count} 步）：{str(outcome.error)[:160]}。"
            "请重新观察页面后继续（可改用单动作逐个处理该段）。",
        )
        return {"success": False, "executed": outcome.executed_count, "error": str(outcome.error or "")}

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
        self._force_next_screenshot = False
        self._consecutive_act_failures = 0
        self._click_xy_fallback_used = False

        # 将目标写入上下文
        self.context.add_turn("user", f"目标: {self.goal}")

        for turn in range(1, self.max_turns + 1):
            self.turns_used = turn
            self._click_xy_fallback_used = False  # one bbox→xy retry per turn
            logger.info("━━━ Turn %d/%d ━━━", turn, self.max_turns)

            # ── Observe ──
            try:
                observation = await self.observe(
                    force_screenshot=self._consecutive_act_failures > 0,
                )
            except Exception as exc:
                logger.exception("observe() 异常 (turn %d)", turn)
                return self._make_result(
                    "error", turn, error=f"观察页面失败: {exc}",
                    start_time=start_time,
                )

            # ── Phase B(auto)：确定性自动成组（机械段一次往返，不用 LLM）──
            if (
                not self._auto_unit_off
                and self._run_steps
                and not self._auto_unit_from_config_disabled()
            ):
                from core.action_unit import build_auto_unit

                _cands = observation.get("candidates") or []
                _unit_actions, _notes = build_auto_unit(
                    self._run_steps, self._journal_covered, _cands,
                    max_steps=self._auto_unit_max(),
                    base_url=self.base_url or None,
                )
                if _unit_actions:
                    logger.info(
                        "unit(auto): 自动成组 %d 步: %s",
                        len(_unit_actions), [a.get("action") for a in _unit_actions],
                    )
                    _unit_out = await self._execute_action_unit(turn, _unit_actions, observation)
                    if _unit_out.get("success"):
                        continue
                    self._auto_unit_off = True
                    continue
                elif _notes:
                    logger.debug("unit(auto): 不成组 — %s", "; ".join(_notes[:3]))

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

            # ── Phase B：宏动作单元（一次往返推进一段机械动作）──
            if isinstance(action.get("unit"), list):
                _unit = await self._execute_action_unit(turn, action.get("unit"), observation)
                if _unit.get("success"):
                    continue
                # 单元失败：已在上下文写入纠偏，进入下一轮重新决策（不计额外动作）
                continue

            # ── Act ──
            try:
                result = await self.act(action)
            except Exception as exc:
                logger.exception("act() 异常 (turn %d)", turn)
                result = {"success": False, "error": str(exc)}

            # ── Act 失败恢复（025-ref-click US3 + Cursor click_xy）──
            if not result.get("success"):
                error = result.get("error", "") or ""
                from core.self_healing import (
                    build_failure_hint,
                    is_stale_ref_error,
                    recover_candidates,
                )

                # Cursor-style: one bbox→click_xy retry after ref click miss
                if not self._click_xy_fallback_used:
                    xy_action = await click_xy_fallback_after_ref_fail(
                        action,
                        mcp_manager=self._mcp_manager,
                        candidates=observation.get("candidates") or self._last_candidates,
                    )
                    if xy_action is not None:
                        self._click_xy_fallback_used = True
                        try:
                            logger.info(
                                "click_xy fallback (turn %d): value=%s",
                                turn, xy_action.get("value"),
                            )
                            action = xy_action
                            thinking_text = action.get("thinking", "") or thinking_text
                            result = await self.act(action)
                        except Exception as exc:
                            logger.warning(
                                "click_xy fallback failed (turn %d): %s", turn, exc,
                            )

                if not result.get("success") and is_stale_ref_error(error) and self._retries_this_run < 1:
                    self._retries_this_run += 1
                    try:
                        fresh_obs = await self.observe(force_screenshot=True)
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
                elif (
                    not result.get("success")
                    and not self._heal_exhausted
                    and not is_stale_ref_error(error)
                ):
                    heal_candidates = await recover_candidates(
                        self._mcp_manager,
                        step_description=self.goal,
                        error=error,
                    )
                    if heal_candidates:
                        try:
                            fresh_obs = await self.observe(force_screenshot=True)
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

            if result.get("success"):
                self._consecutive_act_failures = 0
                # 成功动作入 journal（服务端 OTA 的固化合成输入，形状与 AgentBridge 对齐）
                self._collect_journal_entry(action, turn)
            else:
                self._consecutive_act_failures += 1
                self._force_next_screenshot = True

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
            from core.mcp_args import build_tool_status
            self.context.add_turn("tool", build_tool_status(result))

            # 消息持久化（db + run_id 就绪时写 agent_messages / agent_tool_calls）
            await self._record_turn(turn, observation.get("snapshot", ""), action, result)

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

    def _collect_journal_entry(self, action: dict[str, Any], turn: int) -> None:
        """把一次成功动作记入 journal（供 run 结束后合成固化脚本）。

        与 AgentBridge._collect_journal_entry 形一致；服务端 OTA 没有 checklist 映射，
        ``checklist_index`` 由合成侧按 steps 兜底推断。
        """
        name = str(action.get("action") or action.get("name") or "")
        if not name:
            return
        nested = action.get("args") if isinstance(action.get("args"), dict) else {}
        entry = {
            "turn": turn,
            "success": True,
            "status": "ok",
            "action": name,
            "selector": action.get("selector", nested.get("selector")),
            "value": action.get("value", nested.get("value")),
            "element_desc": (
                action.get("element_desc")
                or action.get("target")
                or nested.get("element_desc")
            ),
        }
        self.journal.append(entry)
        # Phase B(auto)：维护覆盖集合（清单步号）
        try:
            from core.agent_bridge import pick_checklist_index

            idx = pick_checklist_index(self._run_steps, self._journal_covered, entry)
            if idx is not None:
                self._journal_covered.add(int(idx))
        except Exception:
            pass

    async def _record_turn(
        self,
        turn: int,
        snapshot: str,
        action: dict[str, Any],
        result: dict[str, Any],
    ) -> None:
        """将一轮 OTA 记录到 agent_messages / agent_tool_calls。

        仅当构造时传入了 db + run_id 才落库（AgentRunner 被编排器调用时传入，
        纯独立使用时静默跳过）。格式与 AgentBridge._record_turn 对齐：
        user 快照预览 + assistant 决策 + tool_call 结果。
        """
        if self._db is None or self._run_id is None:
            return
        snapshot_str = str(snapshot)
        snapshot_preview = snapshot_str[:2000] + ("..." if len(snapshot_str) > 2000 else "")
        await crud_agent_run.create_message(
            self._db, self._run_id, turn, "user",
            f"[Snapshot] {snapshot_preview}",
        )
        await crud_agent_run.create_message(
            self._db, self._run_id, turn, "assistant",
            str(action),
        )
        # LLM 决策把动作参数放在**顶层**（selector/value/target…），不是嵌套 args ——
        # 曾因此把所有 tool_args 记成 {}，回放里看不到定位符与取值。这里两者都收。
        tool_args = action.get("args") or {
            key: action.get(key)
            for key in (
                "selector", "target", "candidate_ref", "snapshot_version",
                "value", "element_desc", "key", "path", "url", "text",
            )
            if action.get(key) is not None
        }
        await crud_agent_run.create_tool_call(
            self._db, self._run_id, turn,
            action.get("name", action.get("action", "unknown")),
            tool_args,
            result.get("success", False),
            result.get("error", ""),
        )

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
