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
import re
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

# 034：「拒绝 done」同一原因的连续上限（与客户端 bridge 同口径，避免空口 done 或空转）
_REFUSE_DONE_LIMIT = 5


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
    # ── B1 动作面全量接线（2026-10-14）─────────────────────────────────────
    "assert_text",
    "find",
    "console",
    "highlight",
    "hide_highlight",
    "fill_form",
    "network",
    "network_request",
    "resize",
    "emulate_media",
    "mouse_down",
    "mouse_up",
    "close",
    "pdf",
    "video_start",
    "video_stop",
    "video_chapter",
    "video_show_actions",
    "video_hide_actions",
    "trace_start",
    "trace_stop",
    "recording_start",
    "recording_stop",
    "run_code",
    # ── 036 页面 API 调用（系统级窗口/下载受阻时的替代路径）────────────────
    "fetch",
    # ── 036+ 执行链诊断脚本（用例执行中写 Python 查库/调接口/核对数据）──────
    "run_script",
    # ── 修复：B5 verify 四件套漏接服务端注册表（此前服务端链报「未知操作」）──
    "verify_text",
    "verify_visible",
    "verify_value",
    "verify_list",
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
        batch_id: int | None = None,
        evidence_dir: str | None = None,
        screenshot_dir: str | None = None,
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
        # 038 US3：批次停止观察器（DB 轮询 + 进程内标志）
        from core.run_control import BatchStopWatcher

        self._batch_id = batch_id
        self._stop_watcher = BatchStopWatcher(db, batch_id)
        # 036：运行证据目录（fetch 存证落盘用；缺省跳过落盘并如实标注）
        self._evidence_dir = evidence_dir
        # 037 US3：失败现场截图落盘（报告步骤附带现场图；服务端链此前只回喂模型不落盘）
        self._screenshot_dir = screenshot_dir
        self.last_failure_screenshot: str | None = None
        # 036+：执行链 run_script 的批次/身份（首次调用时从批次解析并缓存）
        self._script_user_id: int | None = None
        # 037 US3：失败现场截图（b64 缓存；下一轮 think 无新帧时回喂模型自诊断）
        self._pending_failure_shot_b64: str | None = None
        # 038 US1：坐标回读连续未命中计数
        self._coord_miss_streak = 0

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
        # 034 修复：空口 done 拒绝计数（与客户端 bridge 同口径门禁）
        self._refuse_done_same: int = 0
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
        ) or self._vision_on()
        self._force_next_screenshot = False
        screenshot_b64 = None
        if want_shot:
            screenshot_b64 = await capture_screenshot_b64(self._mcp_manager)
            # 037 US3：失败现场截图落盘（失败轮/强制截图的下一轮），供报告步骤附图
            if (
                screenshot_b64
                and self._screenshot_dir
                and (self._consecutive_act_failures > 0 or force_screenshot)
            ):
                try:
                    import base64 as _b64
                    import os as _os

                    _os.makedirs(self._screenshot_dir, exist_ok=True)
                    _name = f"turn_{self.turns_used:02d}_fail.png"
                    _path = _os.path.join(self._screenshot_dir, _name)
                    _raw = str(screenshot_b64)
                    if _raw.startswith("data:"):
                        _raw = _raw.split(",", 1)[-1]
                    with open(_path, "wb") as _fh:
                        _fh.write(_b64.b64decode(_raw))
                    self.last_failure_screenshot = _path
                except Exception:
                    logger.debug("failure screenshot save skipped", exc_info=True)

        # 037：视觉帧压缩（PNG b64 → JPEG data URL，控 token；异常原样兜底）
        if screenshot_b64:
            try:
                from core.vision_frame import b64_to_frame_data_url

                screenshot_b64 = (
                    b64_to_frame_data_url(screenshot_b64, max_width=self._vision_max_width())
                    or screenshot_b64
                )
            except Exception:
                logger.debug("vision frame compress skipped", exc_info=True)

        return {
            "snapshot": snapshot,
            "url": url,
            "screenshot_b64": screenshot_b64,
            "snapshot_version": snapshot_version(snapshot or ""),
            "candidates": candidates,
        }

    def _vision_max_width(self) -> int:
        try:
            from app.config import get_settings

            return int(getattr(get_settings(), "exec_vision_max_width", 1152) or 1152)
        except Exception:
            return 1152

    def _vision_on(self) -> bool:
        """037：本 run 是否附视觉帧（exec_vision 开关 + 运行时降级）。"""
        if getattr(self, "_vision_off", False):
            return False
        try:
            from app.config import get_settings

            return bool(getattr(get_settings(), "exec_vision", True))
        except Exception:
            return True

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
        # 037 US3：无新帧时回喂失败现场截图（一次性消费；有新帧则作废陈旧失败帧）
        if observation.get("screenshot_b64"):
            self._pending_failure_shot_b64 = None
        elif getattr(self, "_pending_failure_shot_b64", None):
            observation = {**observation, "screenshot_b64": self._pending_failure_shot_b64}
            self._pending_failure_shot_b64 = None

        from core.locator_candidates import serialize_candidates
        step_description += (
            f"\n\nCANDIDATE ELEMENTS (choose selector only from this list):\n"
            f"{serialize_candidates(observation.get('candidates', ())) or '(none)'}"
        )
        if observation.get("screenshot_b64"):
            logger.info(
                "Runner: vision frame attached (%d bytes b64)",
                len(observation.get("screenshot_b64") or ""),
            )
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
        except Exception as exc:
            # 037：网关/模型不支持图片 → 纯文本重试一次并关闭本 run 视觉（探索链同口径）
            if observation.get("screenshot_b64"):
                logger.warning(
                    "Vision think() failed, retry text-only: %s", str(exc)[:200]
                )
                self._vision_off = True
                retry_desc = step_description.replace(
                    "\n\nA VIEWPORT SCREENSHOT is attached for this turn. "
                    "Prefer snapshot refs first; use click_xy from the image when refs are unreliable.",
                    "",
                )
                try:
                    tool_call = await asyncio.wait_for(
                        generate_tool_call(
                            step_description=retry_desc,
                            dom_snapshot=snapshot,
                            client=self._llm_client,
                            model=self._model,
                            system_prompt=self._system_prompt,
                            base_url=self.base_url,
                            screenshot_b64=None,
                            replace_system_prompt=True,
                        ),
                        timeout=100,
                    )
                except Exception:
                    logger.exception("LLM think() 调用失败")
                    return {
                        "action": "error",
                        "selector": None,
                        "value": "LLM 调用失败",
                        "thinking": "思考失败",
                        "next_goal": None,
                    }
            else:
                logger.exception("LLM think() 调用失败")
                return {
                    "action": "error",
                    "selector": None,
                    "value": "LLM 调用失败",
                    "thinking": "思考失败",
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

        # 036+：执行链诊断脚本（服务端沙箱执行，不经 MCP；结果文本回喂模型）
        if str(action_name).lower() in ("run_script", "browser_run_script"):
            return await self._execute_script_action(action)

        # 037：执行链验证码（模型读图识别；服务端链经 ServerBrowser 适配复用识别器）
        if str(action_name).lower() in ("solve_captcha", "browser_solve_captcha"):
            return await self._execute_captcha_action(action)

        result = await self._execute_action_once(action)
        if result.get("success"):
            await self._resolve_coordinate_target(action, result)
            await self._append_coord_feedback(action, result)
        if not result.get("success"):
            from core.mcp_args import annotate_uncertain, is_transient_action_error

            if annotate_uncertain(action, result):
                logger.warning(
                    "Runner: act uncertain（可能已生效，跳过本地重试）: %s",
                    str(result.get("error"))[:100],
                )
            elif is_transient_action_error(result.get("error")):
                for _delay in (0.6, 1.5):
                    logger.info(
                        "Runner: act 瞬时失败，本地重试（%.1fs 后）: %s",
                        _delay, str(result.get("error"))[:80],
                    )
                    await asyncio.sleep(_delay)
                    result = await self._execute_action_once(action)
                    if result.get("success"):
                        break
        # 036：fetch 证据落盘（bytes → 证据文件；路径写回 result.fetch 供 tool_args 记录）
        self._persist_fetch_evidence(result)
        # 037 US3：失败现场截图缓存（b64 压缩后存；下一轮 think 无新帧时回喂自诊断）
        if not result.get("success"):
            try:
                from core.vision_frame import b64_to_frame_data_url

                _raw = await capture_screenshot_b64(self._mcp_manager)
                if _raw:
                    self._pending_failure_shot_b64 = (
                        b64_to_frame_data_url(_raw, max_width=self._vision_max_width()) or _raw
                    )
            except Exception:
                logger.debug("runner failure vision capture skipped", exc_info=True)
        return result

    async def _act_or_unit(
        self, action: dict[str, Any], turn: int, observation: dict[str, Any],
    ) -> dict[str, Any]:
        """恢复路径共用：unit 决策走宏动作单元（此前恢复路径直接 act → 「未知操作: None」）。"""
        if isinstance(action.get("unit"), list):
            unit_out = await self._execute_action_unit(turn, action.get("unit"), observation)
            return {
                "success": bool(unit_out.get("success")),
                "error": unit_out.get("error") or "",
            }
        return await self.act(action)

    def _persist_fetch_evidence(self, result: dict[str, Any]) -> None:
        """036：把 fetch 回传的证据字节写入运行证据目录（缺目录/失败如实标注）。"""
        if not isinstance(result, dict):
            return
        fetch = result.get("fetch")
        if not isinstance(fetch, dict) or not fetch.get("evidence"):
            return
        ev = fetch.pop("evidence")
        if not self._evidence_dir:
            fetch["evidence_error"] = "无证据目录（跳过落盘）"
            return
        from core.fetch_action import save_fetch_evidence_file

        saved = save_fetch_evidence_file(
            self._evidence_dir,
            ev,
            fetch.get("filename"),
            (fetch.get("api") or {}).get("content_type") or "",
        )
        fetch.update(saved)

    async def _append_coord_feedback(self, action: dict[str, Any], result: dict[str, Any]) -> None:
        """038 US1：坐标回读结果写入上下文（下一轮 think 可见）。"""
        from core.coordinate_feedback import (
            COORD_ACTIONS, build_feedback_text, feedback_enabled,
            parse_coordinates, probe_via_mcp,
        )

        if not feedback_enabled():
            return
        name = str(action.get("action") or action.get("name") or "").strip().lower()
        if name not in COORD_ACTIONS:
            return
        nums = parse_coordinates(action.get("value"))
        if len(nums) < 2:
            return
        info, failed = None, True
        if self._mcp_manager is not None:
            info, failed = await probe_via_mcp(self._mcp_manager.call_tool, nums[0], nums[1])
        if info:
            self._coord_miss_streak = 0
        elif not failed:
            self._coord_miss_streak += 1
        text = build_feedback_text(
            info, nums[0], nums[1], streak=self._coord_miss_streak, read_failed=failed,
        )
        action["_hit"] = info or {"hit": False}
        result["text"] = (str(result.get("text") or "") + f"\nCOORD FEEDBACK: {text}").strip()
        self.context.add_turn("user", f"COORD FEEDBACK: {text}")

    async def _resolve_coordinate_target(
        self, action: dict[str, Any], result: dict[str, Any],
    ) -> None:
        """032 US5：服务端坐标动作成功后回解命中元素（失败静默）。"""
        name = str(action.get("action") or action.get("name") or "").strip().lower()
        if name not in ("click_xy", "drag_xy", "browser_mouse_click_xy"):
            return
        try:
            nums = [int(n) for n in re.findall(r"-?\d+", str(action.get("value") or ""))]
            if len(nums) < 2 or self._mcp_manager is None:
                return
            from core.coordinate_probe import probe_via_mcp

            resolved = await probe_via_mcp(self._mcp_manager.call_tool, nums[0], nums[1])
            if resolved:
                action["_resolved"] = resolved
                result["_resolved"] = resolved
        except Exception:
            logger.debug("坐标回解跳过", exc_info=True)

    async def _execute_script_action(self, action: dict[str, Any]) -> dict[str, Any]:
        """036+：执行链 run_script —— 受控沙箱脚本（查库只读 / 平台查询 / HTTP / 读文件）。

        结果文本经 build_tool_status 回喂模型；元数据写回 result["script"]（tool_args 记录）。
        """
        from app.config import get_settings

        if not bool(getattr(get_settings(), "exec_script_enabled", True)):
            return {
                "success": True,
                "text": "诊断脚本功能未开启（exec_script_enabled=false，管理员可在系统设置开启）",
            }
        nested = action.get("args") if isinstance(action.get("args"), dict) else {}
        script = str(action.get("value") or nested.get("value") or "")
        title = str(action.get("element_desc") or action.get("title") or nested.get("element_desc") or "")
        if not script.strip():
            return {"success": True, "text": "脚本内容为空（value 需为 Python 脚本）"}

        from app.services.exec_script import execute_script, resolve_script_user_id

        if self._script_user_id is None:
            self._script_user_id = await resolve_script_user_id(self._db, run_id=self._run_id)
        res = await execute_script(user_id=self._script_user_id, script=script, title=title)

        head = f"诊断脚本「{title}」" if title.strip() else "诊断脚本"
        if res.get("ok"):
            text = (
                f"{head}成功（{res.get('duration_ms')}ms，DB {res.get('db_queries')} / HTTP {res.get('http_calls')}）：\n"
                f"{res.get('output') or '（无输出）'}"
            )
        else:
            text = f"{head}失败：{res.get('error') or '未知错误'}"
        return {"success": True, "text": text[:3000], "script": res}

    async def _execute_captcha_action(self, action: dict[str, Any]) -> dict[str, Any]:
        """037：执行链验证码识别与填写（服务端链）。

        - 开关 exec_captcha_enabled（默认开）；关闭 = 拒绝并给出配置提示
        - 识别器：make_model_captcha_solver（模型读图，无本地 OCR；执行 Agent 模型通道）
        - 浏览器适配：ServerBrowser(mcp_manager)（复用既有 evaluate/fill/click）
        """
        try:
            from app.config import get_settings

            if not bool(getattr(get_settings(), "exec_captcha_enabled", True)):
                return {"success": False, "error": "验证码动作已关闭（exec_captcha_enabled=false）"}
        except Exception:
            pass
        try:
            from core.captcha import make_model_captcha_solver, solve_captcha_via_browser
            from core.chat.browse import ServerBrowser

            browser = ServerBrowser(mcp_manager=self._mcp_manager, headless=True)
            await browser.start()  # 构建 ToolRegistry（act 需要；mcp 已在运行，start 为轻量）

            async def _captcha_llm(messages):
                from app.gen.model_client import call_model

                return await call_model(messages, agent_type="execution")

            solve_fn = make_model_captcha_solver(_captcha_llm)
            # 037 US5：整页截图兜底（非常规验证码：CSS 背景图/SVG/div 装扮）
            page_solve_fn = make_model_captcha_solver(_captcha_llm, page_mode=True)
            ref = action.get("selector") or action.get("ref") or None
            refresh = bool(action.get("refresh") or action.get("value") == "refresh")
            res = await solve_captcha_via_browser(
                browser, input_ref=ref, refresh=refresh, solve_fn=solve_fn,
                page_solve_fn=page_solve_fn,
            )
            if isinstance(res, dict) and res.get("ok"):
                note = str(res.get("note") or "").strip()
                text = f"验证码已识别并填写（{int(res.get('text_len') or 0)} 位）"
                if note:
                    text += f"；{note}"
                return {"success": True, "text": text}
            return {
                "success": False,
                "error": str((res or {}).get("error") or "验证码处理失败")[:200],
            }
        except Exception as exc:
            logger.exception("captcha action failed")
            return {"success": False, "error": f"验证码处理异常: {str(exc)[:160]}"}

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

    def _auto_unit_probe(self) -> bool:
        try:
            from app.config import get_settings

            return bool(getattr(get_settings(), "action_unit_probe", True))
        except Exception:
            return True

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

        from core.action_unit import normalize_unit, run_unit, tool_args_from_action

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

        async def _on_ok(sub_action: dict[str, Any], _result: dict[str, Any]) -> None:
            try:
                self._collect_journal_entry(sub_action, turn)
            except Exception:
                logger.debug("unit journal 记录跳过", exc_info=True)
            if self._db is None or self._run_id is None:
                return
            # 步骤级证据（红线：每动作落库）：逐子动作写 agent_tool_calls，
            # 报告步骤列表与单动作路径口径一致
            try:
                await crud_agent_run.create_tool_call(
                    self._db, self._run_id, turn,
                    str(sub_action.get("action") or "unknown"),
                    tool_args_from_action(sub_action),
                    True,
                    "",
                )
            except Exception:
                logger.debug("unit tool_call 落库跳过", exc_info=True)

        async def _probe_caller(tool: str, args: dict[str, Any]) -> dict[str, Any]:
            return await _exec({"action": "evaluate", "value": args.get("function")})

        async def _probe(target: str):
            from core.browser_probe import probe_fallbacks

            locators, evidence = await probe_fallbacks(_probe_caller, target)
            # 032 US7：0 匹配时诊断"存在但不可见 / 不在 DOM"
            if not locators:
                try:
                    from app.config import get_settings as _gs_h

                    if bool(getattr(_gs_h(), "exec_hidden_probe", True)):
                        from core.browser_probe import diagnose_missing

                        diag = await diagnose_missing(_probe_caller, target)
                        if diag.get("note"):
                            evidence["diagnosis"] = diag["note"]
                except Exception:
                    logger.debug("隐藏元素诊断跳过", exc_info=True)
            return locators, evidence

        _probe_arg = _probe if self._auto_unit_probe() else None
        outcome = await run_unit(steps, _exec, on_step_ok=_on_ok, probe=_probe_arg)
        if outcome.success:
            logger.info("unit: %d 步执行成功（turn %d）", outcome.executed_count, turn)
            self.context.add_turn(
                "assistant", f"宏动作单元：{outcome.executed_count} 步一次往返执行成功",
                tool_calls=list(outcome.executed),
            )
            return {"success": True, "executed": outcome.executed_count}
        # 失败子动作也落库（步骤级证据：报告可定位失败的具体步骤）
        _unit_uncertain = bool(getattr(outcome, "uncertain", False))
        _failed = outcome.failed_action or {}
        # B4：失败步落库前先做遮挡/视口诊断（诊断附入失败步错误文本，报告可见）
        _occ_note = ""
        if _failed and not _unit_uncertain:
            _occ_note = await self._maybe_occlusion_hint(dict(_failed), str(outcome.error or ""), turn) or ""
        if self._db is not None and self._run_id is not None:
            try:
                _failed_args = tool_args_from_action(_failed)
                if _unit_uncertain:
                    _failed_args["_uncertain"] = True
                _err_text = str(outcome.error or "")
                if _occ_note:
                    _err_text = (_err_text[:420] + f" | {_occ_note}")
                await crud_agent_run.create_tool_call(
                    self._db, self._run_id, turn,
                    str(_failed.get("action") or "unknown"),
                    _failed_args,
                    False,
                    _err_text[:500],
                )
            except Exception:
                logger.debug("unit 失败步落库跳过", exc_info=True)
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
        if _unit_uncertain:
            from core.mcp_args import uncertain_context_hint

            logger.warning(
                "unit uncertain（可能已生效）turn %d: %s", turn, str(outcome.error)[:100],
            )
            _hint = uncertain_context_hint(str((outcome.failed_action or {}).get("action") or ""))
            self.context.add_turn("user", _hint)
            if self._db is not None and self._run_id is not None:
                try:
                    await crud_agent_run.create_message(
                        self._db, self._run_id, turn, "user", _hint,
                    )
                except Exception:
                    logger.debug("uncertain 提示消息写入跳过", exc_info=True)
        return {
            "success": False,
            "executed": outcome.executed_count,
            "error": str(outcome.error or ""),
            "uncertain": _unit_uncertain,
        }

    # ── 主循环 ────────────────────────────────────────────────────────────

    async def _maybe_occlusion_hint(self, action: dict[str, Any], error: str, turn: int) -> str | None:
        """B4：点击类动作失败时的遮挡/视口诊断（只读、静默降级、不改成败）。

        返回诊断文本（调用方决定写入步骤错误/报告），并注入下一轮上下文，
        让模型知道"为什么点不中"（被谁遮挡 / 在视口外 / 禁用）。
        """
        try:
            name = str(action.get("action") or action.get("name") or "").strip().lower()
            if name not in ("click", "double_click", "right_click", "check", "hover", "press_key"):
                return None
            from app.config import get_settings

            if not bool(getattr(get_settings(), "exec_occlusion_probe", True)):
                return None
            target = str(action.get("element_desc") or action.get("value") or "").strip()
            selector = str(action.get("selector") or "").strip()
            if selector:
                from core.locator_candidates import is_snapshot_ref

                if is_snapshot_ref(selector):
                    # ref 无法在 JS 中解析——从观察候选里回解可读名称
                    if not target:
                        try:
                            cands = self._last_candidates or []
                            cand = next(
                                (c for c in cands if getattr(c, "ref", None) == selector), None,
                            )
                            if cand is not None:
                                target = str(
                                    getattr(cand, "name", "") or getattr(cand, "text", "") or ""
                                ).strip()
                        except Exception:
                            pass
                    selector = ""
            note = ""
            if selector or target:
                from core.browser_probe import probe_occlusion

                probe = await probe_occlusion(
                    self._mcp_manager.call_tool, target, selector=selector or None,
                )
                note = str(probe.get("note") or "")
            if not note and re.search(r"intercepts pointer|拦截|遮挡", error or "", re.I):
                # 无目标可探时，依据 MCP 错误文本给确定性结论（Playwright 拦截错误）
                note = "目标被页面元素遮挡（点击被拦截——通常为弹窗/遮罩层覆盖）：先关闭/移开遮挡层再操作"
            if not note:
                return None
            hint = f"点击失败诊断（系统自动检测）：{note}"
            self.context.add_turn("user", hint)
            if self._db is not None and self._run_id is not None:
                try:
                    await crud_agent_run.create_message(
                        self._db, self._run_id, turn, "user", hint,
                    )
                except Exception:
                    logger.debug("遮挡诊断提示写入跳过", exc_info=True)
            logger.info("遮挡诊断（turn %d）: %s", turn, note[:100])
            return note
        except Exception:
            logger.debug("遮挡诊断跳过", exc_info=True)
            return None

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
            # 038 US3：每轮开始前检查批次停止（跨 worker 走 DB 轮询）
            if await self._stop_watcher.should_stop():
                logger.info(
                    "AgentRunner: batch %s stopped by user at turn %s",
                    self._batch_id, turn,
                )
                return {
                    "status": "cancelled",
                    "turns_used": turn - 1,
                    "error": "已停止（用户取消）",
                    "result": None,
                }
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

            thinking_text = action.get("thinking") or ""
            logger.info("Thinking: %s", thinking_text[:120])

            # 检查 LLM 是否声明目标已达成
            if action.get("action") == "done":
                summary = action.get("value", "")
                # 034 修复：服务端路径补充与客户端 bridge 同口径的**覆盖门禁**——
                # 有用例步骤时必须 journal 全覆盖，禁止"空口 done"直接判过
                # （实测 case 670：模型把正向断言反转为负向等待后 done，整条假通过）
                if self._run_steps and len(self._journal_covered) < len(self._run_steps):
                    _missed = [
                        int(s.get("step_order") or i + 1)
                        for i, s in enumerate(self._run_steps)
                        if int(s.get("step_order") or i + 1) not in self._journal_covered
                    ]
                    # 断言步骤：按当前快照做确定性验证（命中即覆盖，避免空转）
                    _auto = self._cover_assertions_from_snapshot(
                        _missed, (observation or {}).get("snapshot") or "",
                    )
                    if _auto:
                        logger.info(
                            "AgentRunner auto-covered assertion steps %s from snapshot (turn %d)",
                            _auto, turn,
                        )
                        _missed = [o for o in _missed if o not in _auto]
                    if _missed:
                        self._refuse_done_same += 1
                        if self._refuse_done_same >= _REFUSE_DONE_LIMIT:
                            return self._make_result(
                                "failed", turn,
                                error=(
                                    f"剩余步骤无法完成：{_missed}"
                                    f"（连续 {self._refuse_done_same} 轮 done 被拒且无进展）"
                                ),
                                start_time=start_time,
                            )
                        logger.info(
                            "AgentRunner refusing premature done: uncovered steps %s "
                            "(covered %s/%s, turn %d, refuse=%d)",
                            _missed, len(self._journal_covered), len(self._run_steps),
                            turn, self._refuse_done_same,
                        )
                        self.context.add_turn(
                            "assistant",
                            (
                                "SYSTEM: 完成声明被拒——以下步骤尚无成功动作覆盖："
                                f"{_missed}。不得 done，请继续真实执行（断言步骤需真正执行断言动作）。"
                            ),
                            tool_calls=[action],
                        )
                        continue
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
                            result = await self._act_or_unit(action, turn, fresh_obs)
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
                                result = await self._act_or_unit(action, turn, fresh_obs)
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
                # B4：点击类失败 → 遮挡/视口只读诊断（注入下一轮上下文 + 附入步骤错误）
                _occ_note = await self._maybe_occlusion_hint(
                    action, str(result.get("error") or ""), turn,
                )
                if _occ_note:
                    result["error"] = (
                        str(result.get("error") or "")[:420] + f" | {_occ_note}"
                    )[:600]

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

    # 034：断言步骤快照确定性覆盖（与 bridge._cover_assertion_steps_from_snapshot 同口径）
    _ASSERT_STEP_RE = re.compile(r"^\s*断言")
    _QUOTED_TEXT_RE = re.compile(r"[「【\"']([^」】\"']+)[」】\"']")

    def _cover_assertions_from_snapshot(self, missed: list[int], snapshot: str) -> list[int]:
        """断言步骤：快照中验证到期望文本即记为已覆盖；负向断言不经此自动覆盖。"""
        if not missed or not snapshot:
            return []
        from core.goal_agent_loop import _step_wants_absence

        hay = re.sub(r"\s+", " ", snapshot).lower()
        covered: list[int] = []
        for order in missed:
            rec = next(
                (
                    s for s in (self._run_steps or [])
                    if int(s.get("step_order") or 0) == int(order)
                ),
                None,
            )
            if not rec:
                continue
            desc = str(rec.get("description") or "")
            if not self._ASSERT_STEP_RE.match(desc) or _step_wants_absence(desc):
                continue
            candidates = [m.strip() for m in self._QUOTED_TEXT_RE.findall(desc) if m.strip()]
            expected = str(rec.get("parsed_result") or "").strip()
            if expected and expected not in candidates:
                candidates.append(expected)
            for needle in candidates:
                n = re.sub(r"\s+", " ", str(needle)).strip().lower()
                if not n:
                    continue
                if len(n) <= 2:
                    # 短期望（如徽标数字「1」）：词边界匹配，避免子串假命中
                    if re.search(rf"(?<!\w){re.escape(n)}(?!\w)", hay):
                        self._journal_covered.add(int(order))
                        covered.append(int(order))
                        break
                elif n in hay:
                    self._journal_covered.add(int(order))
                    covered.append(int(order))
                    break
        return covered

    def _collect_journal_entry(self, action: dict[str, Any], turn: int) -> None:
        """把一次成功动作记入 journal（供 run 结束后合成固化脚本）。

        与 AgentBridge._collect_journal_entry 形一致；服务端 OTA 没有 checklist 映射，
        ``checklist_index`` 由合成侧按 steps 兜底推断。
        """
        # 034：有真实进展 → 重置「拒绝 done」计数（与 bridge 同口径）
        self._refuse_done_same = 0
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
            # 034 修复：断言极性覆盖判定需要 wait_mode（text/gone）；
            # 此前缺失导致 wait(gone) 可冒充「断言页面包含【x】」（假通过）
            "wait_mode": action.get("wait_mode") or nested.get("wait_mode"),
        }
        self.journal.append(entry)
        # Phase B(auto)：维护覆盖集合（清单步号）
        try:
            from core.agent_bridge import pick_checklist_index

            idx = pick_checklist_index(self._run_steps, self._journal_covered, entry)
            if idx is not None:
                self._journal_covered.add(int(idx))
                # 2026-10-16：fetch 证据归并到步骤行需要 turn→步骤 映射
                entry["checklist_index"] = int(idx)
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
        action = action or {}
        result = result or {}
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
                "_resolved", "_hit",
            )
            if action.get(key) is not None
        }
        # 036：fetch 的 api 元数据 / 证据路径并入 tool_args（报告标注「以 API 方式完成」）
        _fetch = result.get("fetch") if isinstance(result, dict) else None
        if isinstance(_fetch, dict):
            _api = _fetch.get("api") or {}
            if _api:
                tool_args["api"] = {
                    k: v for k, v in _api.items() if k != "excerpt"
                }
                if _api.get("excerpt"):
                    tool_args["api"]["excerpt"] = str(_api["excerpt"])[:500]
            if _fetch.get("evidence_path"):
                tool_args["evidence_path"] = _fetch["evidence_path"]
                tool_args["evidence_size"] = _fetch.get("evidence_size")
                tool_args["evidence_truncated"] = bool(_fetch.get("evidence_truncated"))
            if _fetch.get("evidence_error"):
                tool_args["evidence_error"] = str(_fetch["evidence_error"])[:200]
        # 036+：诊断脚本元数据并入 tool_args（标题/耗时/计数/错误摘要）
        _script = result.get("script") if isinstance(result, dict) else None
        if isinstance(_script, dict):
            _meta = {
                k: _script.get(k)
                for k in ("ok", "duration_ms", "db_queries", "http_calls", "truncated")
                if _script.get(k) is not None
            }
            if _script.get("error"):
                _meta["error"] = str(_script["error"])[:200]
            tool_args["script"] = _meta
            if _script.get("title"):
                tool_args["title"] = str(_script["title"])[:100]
        await crud_agent_run.create_tool_call(
            self._db, self._run_id, turn,
            str(action.get("name") or action.get("action") or "unknown"),
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
            # 2026-10-16：报告步骤按用例步骤渲染需要覆盖信息（OTA 路径）
            "covered_orders": sorted(int(x) for x in self._journal_covered),
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
