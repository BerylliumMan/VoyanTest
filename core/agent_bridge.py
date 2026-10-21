"""core/agent_bridge.py — Server 端 OTA + 客户端 MCP 桥接编排器

T003: 完整实现 AI Agent 执行编排的 Observe → Think → Act 循环。

AgentBridge 驱动基于 LLM 的自主浏览器代理：
1. Observe — 通过 WebSocket 获取客户端页面快照
2. Think   — LLM 根据目标 + 上下文 + 快照决定下一步操作
3. Act     — 通过 WebSocket 将操作指令发送到客户端执行

循环直到目标达成或达到最大轮次。
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Any

from app.crud import agent_run as crud_agent_run
from app.db_models import AgentRun
from app.runtime_config import resolve_prompt_for_agent
from app.tz import now as tz_now
from core.llm_wrapper import create_openai_client, generate_tool_call
from core.mcp_args import (
    annotate_uncertain,
    uncertain_context_hint,
)
from core.ota_cursor import (
    OTA_CURSOR_SYSTEM_PROMPT,
    bridge_click_xy_act,
    bridge_evaluate_act,
    find_candidate,
    parse_bbox_center_from_text,
    should_capture_screenshot,
)

logger = logging.getLogger(__name__)

# 「拒绝 done」同一原因的连续上限（超过即明确失败，避免 50 失败轮空转）
_REFUSE_DONE_LIMIT = 5


def _exec_vision_enabled() -> bool:
    """037：执行链视觉总开关（读不到配置时默认开，与探索链口径一致）。"""
    try:
        from app.config import get_settings

        return bool(getattr(get_settings(), "exec_vision", True))
    except Exception:
        return True


def pick_checklist_index(
    steps: list[dict] | None,
    covered_orders: set[int],
    entry: dict,
) -> int | None:
    """为成功动作挑选未覆盖的 checklist 步骤序号；找不到类型匹配则返回 None。

    与 nl_goal journal 同语义：只有 ``journal_entry_covers_checklist`` 认可的
    动作才算覆盖（wait/截图 不得冒充 click 步骤），宁缺毋滥 —— 错配条目在
    ``sanitize_journal_for_synth`` 里也会被丢弃。
    """
    from core.goal_agent_loop import coverage_reason, journal_entry_covers_checklist

    first_uncovered: tuple[int, str] | None = None
    for i, s in enumerate(steps or []):
        order = int(s.get("step_order") or s.get("step_number") or i + 1)
        if order in covered_orders:
            continue
        desc = str(s.get("description") or s.get("original_description") or "")
        if first_uncovered is None:
            first_uncovered = (order, desc)
        if journal_entry_covers_checklist(entry, step_description=desc):
            return order
    if first_uncovered is not None:
        # 038 FR-010：拒绝覆盖不静默（诊断原因可查；不影响判定）
        logger.debug(
            "coverage rejected: action=%s reason=%s step=%s desc=%r target=%r",
            entry.get("action"),
            coverage_reason(entry, step_description=first_uncovered[1]),
            first_uncovered[0],
            first_uncovered[1][:80],
            str(entry.get("element_desc") or entry.get("value") or "")[:80],
        )
    return None


class AgentBridge:
    """AI Agent 桥接: Server 端 OTA 循环, 客户端做 MCP 桥接。

    Usage::

        bridge = AgentBridge(agent_manager, db, agent_def)
        run = await bridge.orchestrate(case_id=42, agent_id="agent-1", goal={...})
    """

    def __init__(self, agent_manager, db, agent_def):
        """初始化桥接器。

        Args:
            agent_manager: agent.manager.AgentManager 实例（全局单例）
            db: SQLAlchemy AsyncSession
            agent_def: app.db_models.AgentDefinition 实例
        """
        self.agent_manager = agent_manager
        self.db = db
        self.agent_def = agent_def
        self._notify_user_id: int | None = None
        # 037：视觉运行时状态（网关拒绝图片后置 True → 本 run 剩余轮次纯文本）
        self._vision_off: bool = False
        # 037：验证码断点状态（orchestrate 装载 meta；头段实时 OTA → 尾段固化回放）
        self._breakpoint_until: int | None = None
        self._breakpoint_tail_script: str | None = None
        self._breakpoint_tail_steps: list[dict] = []
        self._breakpoint_replay_logs: list[dict] = []
        # 037 US3：失败现场截图（b64 缓存；下一轮决策无新帧时回喂模型自诊断）
        self._pending_failure_shot_b64: str | None = None
        # 038 US1：坐标回读连续未命中计数
        self._coord_miss_streak = 0

    def _vision_on(self) -> bool:
        """037：本轮是否附视觉帧（exec_vision 开关 + 运行时降级）。"""
        if self._vision_off:
            return False
        return _exec_vision_enabled()

    # ── 主入口 ─────────────────────────────────────────────────────────────

    async def orchestrate(
        self,
        *,
        case_id: int,
        agent_id: str,
        goal: dict,
        existing_run_id: int | None = None,
        environment_id: int | None = None,
        existing_batch_id: int | None = None,
        notify_user_id: int | None = None,
    ) -> AgentRun:
        """OTA 主循环入口。

        Args:
            case_id: 测试用例 ID
            agent_id: Agent 名称（对应 agent_manager 中注册的 id）
            goal: 目标快照（含 type, case_id, agent_name 等字段）
            existing_run_id: 复用已有 AgentRun（跨 worker poller 路径）
            environment_id: 环境 ID，用于获取 base_url
            existing_batch_id: 复用已有 RunBatch（批量执行路径）
            notify_user_id: 完成后给该用户发批次通知（None 则不发）

        Returns:
            已 commit/refresh 的 AgentRun 实例，status 为 completed 或 failed
        """
        # 记录本次执行所属的客户端（execution_mode=client:<agent> 用；此前只在回放分支里设置，
        # 导致回放路径写出 client:unknown）
        self._active_agent_id = agent_id or getattr(self, "_active_agent_id", None)
        # 037：验证码断点状态装载（meta 存在且开关开 → 头段实时 / 尾段回放）
        try:
            await self._load_breakpoint_state(case_id)
        except Exception:
            logger.debug("breakpoint state load skipped", exc_info=True)

        self._notify_user_id = notify_user_id
        if existing_run_id:
            run = await self.db.get(AgentRun, existing_run_id)
            if not run:
                run = await self._create_run(case_id, goal)
            else:
                run.status = "running"
                run.goal = goal
                run.started_at = tz_now()
                await self.db.commit()
        else:
            run = await self._create_run(case_id, goal)
        run_id_str = str(run.id)

        # 固化素材（run 结束成功后合成 compiled_script）：journal = OTA 成功动作轨迹
        self._journal: list[dict] = []
        self._journal_covered: set[int] = set()
        self._run_steps: list[dict] = []
        self._case_url: str = ""
        self._replay_logs: list[dict] | None = None
        self._console_entries: list[str] = []  # 032 US2：console 错误证据（收尾采集）
        # 036+：执行链 run_script 的批次/身份（首次调用时解析并缓存）
        self._script_user_id: int | None = None
        # B2：运行证据（网络问题请求 / 视频）
        self._network_entries: list[str] = []
        self._video_state: dict = {"started": False, "filename": None}
        self._video_web_path: str | None = None
        self._evidence_dir: str | None = None
        self._environment_id = environment_id
        self._reuse_browser_session = bool((goal or {}).get("reuse_browser_session"))
        self._screenshot_dir: str | None = None
        self._screenshot_paths: dict[str, str] = {}
        self._auto_unit_chain_ok = True  # Phase B(auto)：自动成组链路（失败后交给 LLM）
        self._active_agent_id: str | None = None
        self._active_run_id_str: str | None = None
        self._batch_id: int | None = existing_batch_id
        # 038 US3：批次停止观察器（进程内标志 + DB 轮询）
        from core.run_control import BatchStopWatcher

        self._stop_watcher = BatchStopWatcher(self.db, self._batch_id)
        # _fail 已同步置败的预创建 TestRun（_save_report 复用之，防重复 run）
        self._synced_test_run_ids: list[int] = []
        # 注意：037 断点状态在 __init__ 初始化、orchestrate 装载（此处不得重置）

        # 038 US3：进入任何执行分支前先看是否已被停止（回放/编译先行也要让位）
        if await self._stop_watcher.should_stop():
            await self._cancel(
                run, "已停止（用户取消）", agent_id=agent_id, run_id_str=run_id_str,
            )
            try:
                await self._save_report(run, case_id)
            except Exception:
                logger.exception("Bridge cancelled report save failed for run #%d", run.id)
            return run

        # 固化脚本优先：case 有匹配 hash 的 compiled_script 时先回放（秒级、无 LLM），
        # 失败/不支持再回退 OTA 循环（与 nl_goal 链路同语义：回放失败不阻断执行）。
        try:
            if await self._try_replay_compiled_script(run, agent_id, run_id_str):
                await self._collect_console_before_report(agent_id, run_id_str)
                try:
                    await self._save_report(run, case_id)
                except Exception:
                    logger.exception("Bridge report save failed for run #%d", run.id)
                return run
        except Exception:
            logger.warning(
                "Bridge compiled_script replay attempt failed — fallback to OTA",
                exc_info=True,
            )

        # 编译先行（Phase A）：无可回放脚本时，先编译一段脚本直跑（失败不阻断，回落 OTA）
        try:
            if await self._try_compile_first(run, agent_id, run_id_str):
                await self._collect_console_before_report(agent_id, run_id_str)
                try:
                    await self._save_report(run, case_id)
                except Exception:
                    logger.exception("Bridge report save failed for run #%d", run.id)
                return run
        except Exception:
            logger.warning("Bridge 编译先行流程异常 — 回落 OTA", exc_info=True)

        # 发送 RUN_START 让 Agent 启动 MCP / 浏览器
        try:
            await self.agent_manager.send(agent_id, {
                "type": "run_start", "run_id": run_id_str,
                "description": "AI Agent execution",
                "num_steps": 0,
                # 037：验证码断点模式用 hybrid 后端——MCP 附着共享 CDP Chromium，
                # 头段（实时登录）与尾段（固化回放 connect_over_cdp）在同一浏览器，
                # 登录态得以延续（实测：plain MCP 与 CDP 浏览器是两个实例，尾段必败）
                "backend": (
                    "hybrid" if getattr(self, "_breakpoint_until", None) else "playwright_mcp"
                ),
                "reuse_existing_browser": bool(
                    getattr(self, "_reuse_browser_session", False)
                ),
                # 2026-09-30 修复：此前恒为 False → **首个用例**（全新浏览器）也不导航，
                # 页面停在 about:blank，固化脚本回放的第一条断言必然失败（实测 case=513
                # "Locator expected to be visible: get_by_role('heading', name='Products')"），
                # 随后被迫回落 OTA。只有批次内续跑（复用会话）才应跳过导航。
                "navigate_base_url": not bool(
                    getattr(self, "_reuse_browser_session", False)
                ),
            })
            await asyncio.sleep(1)  # 等 MCP 初始化
        except Exception as e:
            logger.warning("Bridge RUN_START failed for run %s: %s", run_id_str, e)

        try:
            import os as _os
            _d = _os.path.join("reports", f"agent_{run.id}_{tz_now().strftime('%Y%m%d_%H%M%S')}", "screenshots")
            _os.makedirs(_d, exist_ok=True)
            self._screenshot_dir = _d
            self._evidence_dir = _os.path.join(_os.path.dirname(_d), "evidence")
        except Exception:
            pass

        # B2：视频证据启动（默认关 exec_video_evidence；旧客户端未知动作 → 静默跳过）
        try:
            from app.config import get_settings as _gs_v

            if bool(getattr(_gs_v(), "exec_video_evidence", False)):
                _fname = f"vt_run_{run.id}_{tz_now().strftime('%H%M%S')}.webm"
                _res = await asyncio.wait_for(
                    self.agent_manager.send_act(
                        agent_id, run_id_str, {"name": "video_start", "args": {"value": _fname}},
                    ),
                    timeout=20,
                )
                if isinstance(_res, dict) and _res.get("success"):
                    self._video_state.update(started=True, filename=_fname)
                    logger.info("Bridge 视频证据已启动（run #%s）", run_id_str)
        except Exception:
            logger.debug("Bridge 视频证据启动跳过", exc_info=True)

        # 立即创建 RunBatch（执行中即可见）
        try:
            await self._create_batch(run, case_id)
        except Exception:
            logger.exception("Bridge batch creation failed for run #%d", run.id)
        try:
            await self._ota_loop(run, agent_id, run_id_str)
        except asyncio.TimeoutError:
            logger.error("Bridge execution timed out for run #%d", run.id)
            await self._fail(run.id, "Bridge execution timed out")
        except Exception:
            logger.exception("Bridge orchestrate failed for run #%d", run.id)
            await self._fail(run.id, "Bridge orchestrate internal error")

        # 032 US2：收尾采集 console 错误（静默降级；并入报告）
        await self._collect_console_before_report(agent_id, run_id_str)

        # 写入报告（TestRun + RunBatch）
        try:
            await self._save_report(run, case_id)
        except Exception:
            logger.exception("Bridge report save failed for run #%d", run.id)

        # 跑成功 → 固化脚本（下次执行优先回放）；合成失败不影响本次结果
        # 门禁：checklist 全覆盖且 journal 非空，避免 LLM 空口 done 后幻觉固化
        if run.status in ("completed", "passed"):
            needed = self._ordered_step_numbers()
            covered = len(self._journal_covered)
            if needed and covered < len(needed):
                logger.info(
                    "Bridge: skip synthesis case=%s (covered %s/%s steps)",
                    case_id, covered, len(needed),
                )
            elif not self._journal:
                logger.info(
                    "Bridge: skip synthesis case=%s (empty journal)", case_id,
                )
            else:
                try:
                    await self._synthesize_compiled_script(run, case_id)
                except Exception:
                    logger.warning(
                        "Bridge: compiled_script synthesis failed case=%s",
                        case_id, exc_info=True,
                    )

        return run

    def _save_screenshot(self, b64_data: str | None, name: str) -> str | None:
        """将 base64 截图保存到文件，返回可供报告页访问的相对路径。"""
        if not b64_data or not self._screenshot_dir:
            return None
        try:
            import base64, os
            path = os.path.join(self._screenshot_dir, f"{name}.png")
            if "," in b64_data:
                b64_data = b64_data.split(",", 1)[1]
            with open(path, "wb") as f:
                f.write(base64.b64decode(b64_data))
            web_path = path.replace("\\", "/")
            self._screenshot_paths[name] = web_path
            return web_path
        except Exception:
            return None

    def _last_screenshot_path(self) -> str | None:
        """失败报告用：优先取 failure/nav 专用图，否则最近一张。"""
        if not self._screenshot_paths:
            return None
        for key in ("failure_final", "nav_fail", "nav_initial"):
            if key in self._screenshot_paths:
                return self._screenshot_paths[key]
        last_key = sorted(self._screenshot_paths.keys())[-1]
        return self._screenshot_paths[last_key]

    async def _collect_console_before_report(self, agent_id: str, run_id_str: str) -> None:
        """032 US2 / B2：报告落库前采集 console 错误 + 网络问题请求 + 视频证据。

        全部经 act 通道一次；任何失败静默降级（旧客户端未知动作同样安全）。
        """
        try:
            from app.config import get_settings

            s = get_settings()
            if not getattr(s, "exec_console_evidence", True):
                pass
            else:
                res = await asyncio.wait_for(
                    self.agent_manager.send_act(
                        agent_id, run_id_str, {"name": "console", "args": {}},
                    ),
                    timeout=30,
                )
                if isinstance(res, dict) and res.get("success"):
                    from core.console_evidence import dedupe_cap, parse_console_messages

                    text = str(res.get("text") or res.get("result") or "")
                    kept, _ = dedupe_cap(
                        parse_console_messages(text),
                        int(getattr(s, "exec_console_max_entries", 20) or 20),
                    )
                    self._console_entries = kept
                    if kept:
                        logger.info(
                            "Bridge console 证据: %d 条（run #%s）", len(kept), run_id_str,
                        )
        except Exception:
            logger.debug("Bridge console 证据采集跳过", exc_info=True)

        # B2：网络证据（问题请求摘要；默认开）
        try:
            from app.config import get_settings as _gs_n

            s_n = _gs_n()
            if bool(getattr(s_n, "exec_network_evidence", True)):
                from core.run_evidence import network_problem_lines

                cap_n = int(getattr(s_n, "exec_network_max_entries", 10) or 10)
                res_n = await asyncio.wait_for(
                    self.agent_manager.send_act(
                        agent_id, run_id_str, {"name": "network", "args": {}},
                    ),
                    timeout=30,
                )
                if isinstance(res_n, dict) and res_n.get("success"):
                    lines = network_problem_lines(
                        str(res_n.get("text") or res_n.get("result") or ""), cap_n,
                    )
                    self._network_entries = lines
                    if lines:
                        logger.info(
                            "Bridge 网络证据: %d 条（run #%s）", len(lines), run_id_str,
                        )
        except Exception:
            logger.debug("Bridge 网络证据采集跳过", exc_info=True)

        # B2：视频证据（停止 → 客户端回传 base64 → 落盘 run 证据目录）
        if self._video_state.get("started"):
            try:
                from core.run_evidence import (
                    parse_video_filename,
                    save_b64_file,
                    web_path_for,
                )

                res_v = await asyncio.wait_for(
                    self.agent_manager.send_act(
                        agent_id, run_id_str, {"name": "video_stop", "args": {}},
                    ),
                    timeout=60,
                )
                if isinstance(res_v, dict) and res_v.get("success"):
                    fname = (
                        str(res_v.get("file_name") or "")
                        or parse_video_filename(str(res_v.get("text") or ""))
                        or self._video_state.get("filename")
                        or "video.webm"
                    )
                    dest = os.path.join(
                        self._evidence_dir or os.path.join("reports", "evidence"),
                        os.path.basename(fname),
                    )
                    saved = save_b64_file(res_v.get("file_b64"), dest)
                    if saved:
                        self._video_web_path = web_path_for(saved)
                        logger.info(
                            "Bridge 视频证据已保存: %s（run #%s）", self._video_web_path, run_id_str,
                        )
            except Exception:
                logger.debug("Bridge 视频证据收尾跳过", exc_info=True)

    async def _resolve_coordinate_target(
        self, agent_id: str, run_id_str: str, action: dict[str, Any],
        result: dict[str, Any],
    ) -> None:
        """032 US5：click_xy/drag_xy 成功后回解命中元素（写入 args._resolved）。

        任何失败静默降级（不阻塞、不改变动作成败）。
        """
        name = str(action.get("name") or action.get("action") or "").strip().lower()
        if name not in ("click_xy", "drag_xy", "browser_mouse_click_xy"):
            return
        args = action.get("args")
        if not isinstance(args, dict):
            args = action
        value = str(args.get("value") or "")
        nums = [int(n) for n in re.findall(r"-?\d+", value)]
        if len(nums) < 2:
            return
        try:
            from core.coordinate_probe import probe_via_evaluate_act

            async def _send_act(sub_action: dict[str, Any]) -> dict[str, Any]:
                return await asyncio.wait_for(
                    self.agent_manager.send_act(agent_id, run_id_str, sub_action),
                    timeout=30,
                )

            resolved = await probe_via_evaluate_act(_send_act, nums[0], nums[1])
            if resolved:
                args["_resolved"] = resolved
                result["_resolved"] = resolved
                logger.info(
                    "Bridge: 坐标回解 → %s/%s（run #%s）",
                    resolved.get("role") or "?",
                    str(resolved.get("name") or resolved.get("selector") or "")[:60],
                    run_id_str,
                )
        except Exception:
            logger.debug("Bridge 坐标回解跳过", exc_info=True)

    async def _append_coord_feedback(
        self, agent_id: str, run_id_str: str,
        action: dict[str, Any], result: dict[str, Any],
        context_messages: list[dict[str, str]],
    ) -> None:
        """038 US1：坐标回读结果注入下一轮决策（含连续点偏提示）。"""
        from core.coordinate_feedback import (
            COORD_ACTIONS, build_feedback_text, parse_coordinates, probe_via_act,
        )

        name = str(action.get("name") or action.get("action") or "").strip().lower()
        if name not in COORD_ACTIONS:
            return
        args = action.get("args") if isinstance(action.get("args"), dict) else action
        nums = parse_coordinates(args.get("value"))
        if len(nums) < 2:
            return
        try:
            async def _send(sub_action: dict[str, Any]) -> dict[str, Any]:
                return await asyncio.wait_for(
                    self.agent_manager.send_act(agent_id, run_id_str, sub_action),
                    timeout=30,
                )

            info, failed = await probe_via_act(_send, nums[0], nums[1])
        except Exception:
            info, failed = None, True
        if info:
            self._coord_miss_streak = 0
        elif not failed:
            self._coord_miss_streak += 1
        text = build_feedback_text(
            info, nums[0], nums[1], streak=self._coord_miss_streak, read_failed=failed,
        )
        action.setdefault("args", {})["_hit"] = info or {"hit": False}
        result["text"] = (str(result.get("text") or "") + f"\nCOORD FEEDBACK: {text}").strip()
        context_messages.append({"role": "user", "content": f"COORD FEEDBACK: {text}"})

    async def _enrich_failure_evidence(
        self, agent_id: str, run_id_str: str, action: dict[str, Any],
        result: dict[str, Any], snapshot: str | None = None,
    ) -> None:
        """032 US6/US7：动作失败时补充证据（不改变成败）。

        - US6：断言/等待失败且客户端 caps 含 browser_find → find 定向查找，附命中上下文；
        - US7：目标未找到类失败 → 隐藏元素诊断（hidden / not_in_dom）。
        """
        try:
            from app.config import get_settings

            s = get_settings()
        except Exception:
            s = None
        name = str(action.get("name") or action.get("action") or "").strip().lower().replace("browser_", "")
        args = action.get("args") if isinstance(action.get("args"), dict) else action
        error = str(result.get("error") or "")

        # US6：find 辅助（仅断言/等待失败，且能力可用）
        if s is not None and bool(getattr(s, "exec_find_probe", True)) and name in ("assert_text", "wait"):
            try:
                caps = set(self.agent_manager.get_agent_caps(agent_id) or set())
            except Exception:
                caps = set()
            if "browser_find" in caps:
                token = str(args.get("value") or "").strip()
                if token:
                    try:
                        res = await asyncio.wait_for(
                            self.agent_manager.send_act(
                                agent_id, run_id_str, {"name": "find", "args": {"value": token}},
                            ),
                            timeout=30,
                        )
                        text = str((res or {}).get("text") or "").strip()
                        if text:
                            result["error"] = (error[:420] + f" | find: {text[:200]}")[:600]
                            error = result["error"]
                            logger.info("Bridge find 辅助: %s（run #%s）", text[:80], run_id_str)
                    except Exception:
                        logger.debug("find 辅助跳过", exc_info=True)

        # US7：隐藏元素诊断（仅目标未找到类失败）
        if s is not None and bool(getattr(s, "exec_hidden_probe", True)) and re.search(
            r"not found|no node|not visible|no element|元素不存在|未找到|不可见", error, re.I
        ):
            target = str(args.get("element_desc") or args.get("value") or "").strip()
            if target:
                try:
                    async def _caller(tool: str, payload: dict) -> dict:
                        return await asyncio.wait_for(
                            self.agent_manager.send_act(
                                agent_id, run_id_str,
                                {"name": "evaluate", "args": {"value": payload.get("function")}},
                            ),
                            timeout=30,
                        )

                    from core.browser_probe import diagnose_missing

                    diag = await diagnose_missing(_caller, target)
                    if diag.get("note"):
                        result["error"] = (error[:420] + f" | {diag['note']}")[:600]
                        logger.info("Bridge 隐藏元素诊断: %s（run #%s）", diag["note"][:90], run_id_str)
                except Exception:
                    logger.debug("隐藏元素诊断跳过", exc_info=True)

        # B4：点击类失败 → 遮挡/视口诊断（只读，结果附到错误文本供下一轮参考）
        if (
            s is not None
            and bool(getattr(s, "exec_occlusion_probe", True))
            and name in ("click", "double_click", "right_click", "check", "hover", "press_key")
        ):
            target = str(args.get("element_desc") or args.get("value") or "").strip()
            selector = str(args.get("selector") or "").strip()
            if selector:
                try:
                    from core.locator_candidates import is_snapshot_ref

                    if is_snapshot_ref(selector):
                        # ref 无法在 JS 中解析——从快照候选里回解可读名称
                        if not target and snapshot:
                            from core.locator_candidates import (
                                actionable_candidates,
                                extract_candidates,
                                find_candidate,
                            )

                            cand = find_candidate(
                                actionable_candidates(extract_candidates(snapshot)),
                                selector,
                            )
                            if cand is not None:
                                target = str(
                                    getattr(cand, "name", "") or getattr(cand, "text", "") or ""
                                ).strip()
                        selector = ""
                except Exception:
                    selector = ""
            note = ""
            if selector or target:
                try:
                    async def _caller(tool: str, payload: dict) -> dict:
                        return await asyncio.wait_for(
                            self.agent_manager.send_act(
                                agent_id, run_id_str,
                                {"name": "evaluate", "args": {"value": payload.get("function")}},
                            ),
                            timeout=30,
                        )

                    from core.browser_probe import probe_occlusion

                    probe = await probe_occlusion(_caller, target, selector=selector or None)
                    note = str(probe.get("note") or "")
                except Exception:
                    logger.debug("遮挡诊断跳过", exc_info=True)
            if not note and re.search(r"intercepts pointer|拦截|遮挡", str(result.get("error") or ""), re.I):
                note = "目标被页面元素遮挡（点击被拦截——通常为弹窗/遮罩层覆盖）：先关闭/移开遮挡层再操作"
            if note:
                result["error"] = (str(result.get("error") or "")[:420] + f" | {note}")[:600]
                logger.info("Bridge 遮挡诊断: %s（run #%s）", note[:90], run_id_str)

    async def _capture_failure_screenshot(
        self, agent_id: str, run_id_str: str, name: str = "failure_final",
        target: str | None = None,
    ) -> str | None:
        """失败终止前强制 observe+截图，保证报告有现场图。

        032 US3：target（快照 ref/CSS）且开关开启时优先元素级截图+高亮；
        客户端/工具不支持时自动回退整页（旧客户端忽略新字段）。
        """
        try:
            _element_mode = False
            try:
                from app.config import get_settings
                _element_mode = (
                    bool(getattr(get_settings(), "exec_element_screenshot", True))
                    and bool(target)
                )
            except Exception:
                _element_mode = bool(target)
            obs = await asyncio.wait_for(
                self.agent_manager.send_observe(
                    agent_id, run_id_str, want_screenshot=True,
                    screenshot_target=(target if _element_mode else None),
                    screenshot_highlight=_element_mode,
                ),
                timeout=30,
            )
            if isinstance(obs, dict):
                b64 = obs.get("screenshot_b64")
                if b64:
                    # 037 US3：保留失败帧供下一轮决策回喂（无新帧时消费一次）
                    self._pending_failure_shot_b64 = b64
                return self._save_screenshot(b64, name)
        except Exception:
            logger.debug(
                "Bridge failure screenshot capture skipped (%s)", name, exc_info=True,
            )
        return None

    async def _create_batch(self, run: AgentRun, case_id: int) -> None:
        """执行前创建 RunBatch，标记为 running。仅当未指定 existing_batch_id 时创建。"""
        if self._batch_id:
            logger.debug("Bridge: reuse existing batch #%d", self._batch_id)
            return
        from app.crud import get_test_case
        from app.db_models import RunBatch
        tc = await get_test_case(self.db, case_id)
        batch = RunBatch(status="running", triggered_by=f"agent:{self.agent_def.name}",
                         project_id=tc.project_id if tc else 0, total_cases=1,
                         started_at=tz_now(),
                         name=(tc.name if tc and tc.name else ""))
        self.db.add(batch)
        await self.db.commit()
        await self.db.refresh(batch)
        self._batch_id = batch.id
        logger.info("Bridge: batch #%d created (running)", batch.id)

    def _client_execution_mode(self, run: "AgentRun | None" = None) -> str:
        """客户端执行模式标识（029 约定：client:<agent_name>）。

        取值顺序：本次执行记录的 agent → run.goal 里的 agent_name（客户端派发时必带）→ unknown。
        回放（compiled_script）路径在设置 _active_agent_id 之前就可能落报告，故这里兜底。
        """
        name = getattr(self, "_active_agent_id", None)
        if not name and run is not None:
            goal = getattr(run, "goal", None)
            if isinstance(goal, str):
                try:
                    import json as _json

                    goal = _json.loads(goal)
                except Exception:
                    goal = None
            if isinstance(goal, dict):
                name = goal.get("agent_name")
        return f"client:{name or 'unknown'}"

    async def _save_report(self, run: AgentRun, case_id: int) -> None:
        """创建 TestRun / RunBatch 报告记录，含步骤日志。"""
        from core.runner._persistence import save_run_results
        from app.tz import now as tz_now
        from app.crud import get_test_case
        from app.db_models import RunBatch

        # 创建或更新 RunBatch
        tc = await get_test_case(self.db, case_id)
        if run.status in ("completed", "passed"):
            run_status_map = "passed"
        elif run.status in ("cancelled", "stopped"):
            run_status_map = "cancelled"
        else:
            run_status_map = "failed"
        batch = None
        if self._batch_id:
            from sqlalchemy import select as _sl
            from app.db_models import RunBatch as _RB
            r = await self.db.execute(_sl(_RB).where(_RB.id == self._batch_id))
            batch = r.scalar_one_or_none()
            if batch:
                # 038 US3：已被用户停止的批次不得被本 run 的终态覆盖
                if batch.status in ("cancelled", "stopping"):
                    run_status_map = "cancelled"
                else:
                    batch.status = run_status_map
                    batch.passed = 1 if run_status_map == "passed" else 0
                    batch.failed = 1 if run_status_map == "failed" else 0
        if not self._batch_id or not batch:
            batch = RunBatch(
                status=run_status_map,
                triggered_by=f"agent:{self.agent_def.name}",
                project_id=tc.project_id if tc else 0,
                total_cases=1,
                started_at=getattr(run, "started_at", None) or tz_now(),
                passed=1 if run_status_map == "passed" else 0,
                failed=1 if run_status_map == "failed" else 0,
                name=(tc.name if tc and tc.name else ""),
            )
            self.db.add(batch)
        await self.db.commit()
        await self.db.refresh(batch)
        self._batch_id = batch.id
        if self._batch_id and self._notify_user_id:
            try:
                from app.services.notifications import notify_batch_completed
                await notify_batch_completed(self._batch_id, self._notify_user_id)
            except Exception:
                logger.exception("Bridge batch notify failed batch=%s", self._batch_id)

        # 工具调用行（失败截图解析；fetch 证据行备用）——渲染口径统一在 core/step_logs.py
        tool_rows: list[dict] = []
        try:
            from core.step_logs import build_step_logs_from_tool_calls

            def _resolve_ss(turn_number: int):
                return (
                    self._screenshot_paths.get(f"turn_{turn_number:02d}_observe")
                    or self._screenshot_paths.get(f"turn_{turn_number:02d}_act")
                    or self._last_screenshot_path()
                )

            tool_rows = await build_step_logs_from_tool_calls(
                self.db, run.id, resolve_screenshot=_resolve_ss
            )
        except Exception as _tce:
            logger.warning("Could not load tool calls for run %d: %s", run.id, _tce)

        # 报告步骤口径（2026-10-16 用户实测：OTA 路径展开用例应显示**用例步骤**，
        # 而非 goto/click 等原始动作行——与固化回放路径同口径）：
        #   固化回放 → 回放步骤行；OTA → 用例步骤行（+ 页面 API 证据行）；
        #   无用例步骤 → 工具行兜底。
        logs: list[dict] = []
        case_step_mode = False
        if self._replay_logs:
            logs = list(self._replay_logs)
        elif getattr(self, "_run_steps", None):
            from core.step_logs import build_case_step_logs

            case_step_mode = True
            run_ok = run.status in ("completed", "passed")
            logs = build_case_step_logs(
                self._run_steps, self._journal_covered, run_ok, run.error,
            )
            # 页面 API（fetch）证据并入其覆盖的步骤行（含存证路径；重复调用去重；
            # 无法映射的残余行原样保留）
            from core.step_logs import (
                fetch_evidence_rows,
                merge_fetch_evidence_into_steps,
                script_evidence_rows,
            )

            logs += merge_fetch_evidence_into_steps(
                logs, fetch_evidence_rows(tool_rows), self._journal,
            )
            # 036+：诊断脚本证据行（执行中写代码取证；标题/耗时/计数）
            logs += script_evidence_rows(tool_rows)
            # 037：验证码断点尾段回放行（头段用例步骤 + 尾段回放行同屏）
            if getattr(self, "_breakpoint_replay_logs", None):
                logs += list(self._breakpoint_replay_logs)
        else:
            logs = tool_rows

        # 032 US2：console 证据并入步骤日志（失败步骤合并 / 运行级汇总行）
        if self._console_entries:
            try:
                from app.config import get_settings as _get_settings
                from core.console_evidence import apply_console_evidence

                apply_console_evidence(
                    logs, self._console_entries,
                    int(getattr(_get_settings(), "exec_console_max_entries", 20) or 20),
                )
            except Exception:
                logger.debug("console 证据合并跳过", exc_info=True)

        # B2：网络问题请求证据（与 console 同一种合并式归属）
        if self._network_entries:
            try:
                from core.run_evidence import apply_network_evidence

                apply_network_evidence(logs, self._network_entries)
            except Exception:
                logger.debug("网络证据合并跳过", exc_info=True)

        # B2：视频证据（运行级信息行，路径可点击）
        if self._video_web_path:
            try:
                from core.run_evidence import apply_video_evidence

                apply_video_evidence(logs, self._video_web_path)
            except Exception:
                logger.debug("视频证据合并跳过", exc_info=True)

        last_shot = self._last_screenshot_path()
        # 无工具调用但失败时，写一条错误日志（挂上失败现场截图）
        if not logs and run.status in ("failed", "error"):
            err_msg = run.error or "执行失败，无详细步骤记录"
            logs.append({
                "step_id": None,
                "level": "error",
                "message": err_msg,
                "screenshot_path": last_shot,
            })
        # 失败跑且没有任何步骤带图 → 挂到最后一条 error（或追加）
        elif run.status in ("failed", "error") and last_shot:
            if not any(l.get("screenshot_path") for l in logs):
                for l in reversed(logs):
                    if l.get("level") == "error":
                        l["screenshot_path"] = last_shot
                        break
                else:
                    logs.append({
                        "step_id": None,
                        "level": "error",
                        "message": run.error or "执行失败",
                        "screenshot_path": last_shot,
                    })

        # 调用 save_run_results 创建 TestRun
        # 若 _fail 已同步置败预创建的 TestRun（027 兜底），复用同一行避免重复 run
        _synced = getattr(self, "_synced_test_run_ids", None) or []
        _reuse_run_id = _synced[0] if len(_synced) == 1 else None
        # 用例步骤口径下：预置的通用「执行失败:」行冗余（失败原因已并入失败步骤行）→ 清理
        if _reuse_run_id and case_step_mode:
            try:
                from sqlalchemy import text as _text_del

                await self.db.execute(
                    _text_del(
                        "DELETE FROM run_logs WHERE run_id=:r AND level='error' "
                        "AND message LIKE '执行失败:%'"
                    ),
                    {"r": _reuse_run_id},
                )
                await self.db.commit()
            except Exception:
                logger.debug("清理预置失败行跳过", exc_info=True)
        saved_run_id = await save_run_results(
            case_id=case_id,
            status=run_status_map,
            start_time=run.started_at or tz_now(),
            end_time=run.completed_at or tz_now(),
            duration=((run.completed_at or tz_now()) - (run.started_at or tz_now())).total_seconds(),
            report_path=None,
            log_path=None,
            logs=logs,
            batch_id=batch.id,
            run_id=_reuse_run_id,
        )
        # 2026-09-30 修复：客户端执行的报告此前 execution_mode 落到列默认值 "server"（实际跑在客户端）。
        # 与 029 接口客户端执行同一约定：client:<agent>。
        if saved_run_id:
            try:
                from core.runner._api_execution import _persist_execution_mode

                await _persist_execution_mode(
                    self.db, int(saved_run_id), self._client_execution_mode(run)
                )
            except Exception:
                logger.warning(
                    "Bridge: execution_mode 写入失败 run=%s", saved_run_id, exc_info=True
                )
        logger.info("Bridge: report saved for run #%d (batch #%d, %d steps)", run.id, batch.id, len(logs))

    def _auto_unit_actions(self, snapshot: str) -> list[dict[str, Any]] | None:
        """Phase B(auto)：把"剩余连续机械步骤"确定性组装成 unit（不用模型）。"""
        try:
            from app.config import get_settings

            cfg = get_settings()
            if not (
                getattr(cfg, "action_unit_enabled", True)
                and getattr(cfg, "action_unit_auto", True)
                and getattr(self, "_auto_unit_chain_ok", True)
            ):
                return None
            if not getattr(self, "_run_steps", None):
                return None
            from core.action_unit import build_auto_unit
            from core.locator_candidates import actionable_candidates, extract_candidates

            cands = actionable_candidates(extract_candidates(snapshot or ""))
            actions, notes = build_auto_unit(
                self._run_steps,
                getattr(self, "_journal_covered", set()),
                cands,
                max_steps=int(getattr(cfg, "action_unit_max", 6) or 6),
                base_url=getattr(self, "_case_url", "") or None,
            )
            if not actions:
                if notes:
                    logger.debug("Bridge unit(auto): 不成组 — %s", "; ".join(notes[:3]))
                return None
            logger.info(
                "Bridge unit(auto): 自动成组 %d 步: %s",
                len(actions), [a.get("action") for a in actions],
            )
            return actions
        except Exception:
            logger.debug("Bridge unit(auto): 组装失败（跳过）", exc_info=True)
            return None

    async def _execute_action_unit(
        self, run: AgentRun, turn: int, agent_id: str, run_id_str: str,
        unit_raw: Any, snapshot: str, context_messages: list[dict[str, str]],
    ) -> dict[str, Any]:
        """Phase B：执行一个"宏动作单元"（多子动作、逐步入账、失败原地停止）。

        返回 {"success": bool, "executed": int}；不为 None 表示已按单元路径处理完毕，
        调用方按成功/失败分别处理（成功进入下一轮 observe；失败注入纠偏提示）。
        """
        from app.config import get_settings

        _t0 = time.monotonic()
        s = get_settings()
        if not getattr(s, "action_unit_enabled", True):
            return {"success": False, "executed": 0, "disabled": True}
        max_steps = int(getattr(s, "action_unit_max", 6) or 6)

        # ref 校验：只认本轮候选里的 ref；CSS/text= 等直通（与既有语义一致）
        try:
            from core.locator_candidates import (
                actionable_candidates,
                extract_candidates,
                is_snapshot_ref,
            )

            _refs = {
                c.ref for c in actionable_candidates(extract_candidates(snapshot or ""))
            }
        except Exception:
            _refs = set()

        def _valid_ref(sel: str) -> bool:
            if not is_snapshot_ref(sel or ""):
                return True
            return sel in _refs

        from core.action_unit import normalize_unit, run_unit, tool_args_from_action

        steps, problems = normalize_unit(
            unit_raw, max_steps=max_steps, is_valid_ref=_valid_ref
        )
        if problems:
            logger.info("Bridge unit: 规范化丢弃 %s（run #%d turn %d）", problems, run.id, turn)
        if not steps:
            context_messages.append({
                "role": "assistant",
                "content": (
                    "SYSTEM: 你的 unit 没有任何可执行子动作（"
                    + "; ".join(problems[:3])
                    + "）。请改用普通单动作或修正后重发。"
                ),
            })
            return {"success": False, "executed": 0, "invalid": True}

        try:
            from app.config import get_settings as _gs_el_u
            _el_ss = bool(getattr(_gs_el_u(), "exec_element_screenshot", True))
        except Exception:
            _el_ss = True

        async def _exec(sub_action: dict[str, Any]) -> dict[str, Any]:
            try:
                return await asyncio.wait_for(
                    self.agent_manager.send_act(agent_id, run_id_str, {**sub_action, "_element_screenshot": _el_ss}),
                    timeout=120,
                )
            except asyncio.TimeoutError:
                return {"success": False, "error": "Act timeout (120s)"}
            except Exception as exc:  # noqa: BLE001
                return {"success": False, "error": str(exc)}

        async def _on_ok(sub_action: dict[str, Any], _result: dict[str, Any]) -> None:
            try:
                self._collect_journal_entry(sub_action, turn)
            except Exception:
                logger.debug("Bridge unit journal 记录跳过", exc_info=True)
            # 037：记录最近子动作（断点 should_stop 判定用）
            _last_sub["name"] = str(sub_action.get("action") or "").lower()
            # 步骤级证据（红线：每动作落库）：逐子动作写 agent_tool_calls，
            # 报告步骤列表与单动作路径口径一致（此前只进合成 journal，报告缺步骤）
            try:
                await crud_agent_run.create_tool_call(
                    self.db, run.id, turn,
                    str(sub_action.get("action") or "unknown"),
                    tool_args_from_action(sub_action),
                    True,
                    "",
                )
            except Exception:
                logger.debug("Bridge unit tool_call 落库跳过", exc_info=True)
            # 上下文历史：与单动作路径同口径——模型下一轮 HISTORY 能看到子动作
            # 及其工具结果，避免"看不见已成功→重复执行"空转（case 551 曾 24 轮）
            try:
                context_messages.append({
                    "role": "assistant",
                    "content": (
                        f"{sub_action.get('action', '?')}: "
                        f"{tool_args_from_action(sub_action)}"
                    ),
                })
                from core.mcp_args import build_tool_status
                context_messages.append({
                    "role": "tool",
                    "content": build_tool_status(_result or {}),
                })
            except Exception:
                logger.debug("Bridge unit 上下文记录跳过", exc_info=True)

        async def _probe_caller(tool: str, args: dict[str, Any]) -> dict[str, Any]:
            return await _exec({"action": "evaluate", "value": args.get("function")})

        async def _probe(target: str):
            from core.browser_probe import probe_fallbacks

            locators, evidence = await probe_fallbacks(_probe_caller, target)
            # 032 US7：0 匹配时诊断"存在但不可见 / 不在 DOM"（并入证据与失败文案）
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

        _probe_arg = _probe if bool(getattr(s, "action_unit_probe", True)) else None

        # 037：验证码断点——头段（含验证码）就绪且本子动作为"提交类"（click/press/
        # solve_captcha/dialog/select）时提前收束，剩余子动作由尾段回放承担
        _last_sub: dict[str, str] = {"name": ""}

        def _should_stop() -> bool:
            if not getattr(self, "_breakpoint_until", None):
                return False
            if not self._journal_head_covered():
                return False
            if len(self._journal_covered) >= len(self._run_steps or []):
                return False  # 尾段也已覆盖：走常规完成路径
            return _last_sub["name"] in ("click", "press_key", "solve_captcha", "dialog", "select")

        outcome = await run_unit(
            steps, _exec, on_step_ok=_on_ok, probe=_probe_arg, should_stop=_should_stop,
        )
        if outcome.success:
            logger.info(
                "Bridge unit: %d 步执行成功（run #%d turn %d，%.1fs）",
                outcome.executed_count, run.id, turn, time.monotonic() - _t0,
            )
            try:
                await crud_agent_run.create_message(
                    self.db, run.id, turn, "assistant",
                    f"宏动作单元：{outcome.executed_count} 步一次往返执行成功",
                )
            except Exception:
                logger.debug("unit 审计消息写入跳过", exc_info=True)
            return {
                "success": True,
                "executed": outcome.executed_count,
                "last_action": (
                    str((outcome.executed[-1] or {}).get("action") or "")
                    if outcome.executed else ""
                ),
                # 037：验证码断点提前收束标记（头段就绪，尾段走回放）
                "stopped_early": bool(getattr(outcome, "stopped_early", False)),
            }

        # 失败子动作也落库（步骤级证据：报告可定位失败的具体步骤）
        _unit_uncertain = bool(getattr(outcome, "uncertain", False))
        _failed = outcome.failed_action or {}
        _err_text = str(outcome.error or "")
        # B4：失败步落库前先做遮挡/视口诊断（与单动作路径同口径，诊断附入错误文本）
        if _failed and not _unit_uncertain:
            try:
                _scratch = {"error": _err_text}
                await self._enrich_failure_evidence(
                    agent_id, run_id_str, dict(_failed), _scratch, snapshot,
                )
                _enriched = str(_scratch.get("error") or "")
                if _enriched != _err_text:
                    _err_text = _enriched
            except Exception:
                logger.debug("Bridge unit 失败证据补充跳过", exc_info=True)
        try:
            _failed_args = tool_args_from_action(_failed)
            if _unit_uncertain:
                _failed_args["_uncertain"] = True
            await crud_agent_run.create_tool_call(
                self.db, run.id, turn,
                str(_failed.get("action") or "unknown"),
                _failed_args,
                False,
                _err_text[:500],
            )
        except Exception:
            logger.debug("Bridge unit 失败步落库跳过", exc_info=True)

        logger.info(
            "Bridge unit: 第 %d 步失败停止（已完成 %d 步）: %s（run #%d turn %d，%.1fs）",
            (outcome.failed_index or 0) + 1, outcome.executed_count,
            str(outcome.error)[:100], run.id, turn, time.monotonic() - _t0,
        )
        context_messages.append({
            "role": "assistant",
            "content": (
                f"SYSTEM: 宏动作单元在第 {(outcome.failed_index or 0) + 1} 步失败"
                f"（已完成 {outcome.executed_count} 步）：{_err_text[:200]}。"
                "请重新观察页面后继续（可改用单动作逐个处理该段）。"
            ),
        })
        if _unit_uncertain:
            logger.warning(
                "Bridge unit uncertain（可能已生效）run #%d turn %d: %s",
                run.id, turn, str(outcome.error)[:100],
            )
            _hint = uncertain_context_hint(
                str((outcome.failed_action or {}).get("action") or "")
            )
            context_messages.append({"role": "user", "content": _hint})
            try:
                await crud_agent_run.create_message(
                    self.db, run.id, turn, "user", _hint,
                )
            except Exception:
                logger.debug("uncertain 提示消息写入跳过", exc_info=True)
        return {
            "success": False,
            "executed": outcome.executed_count,
            "error": _err_text,
            "uncertain": _unit_uncertain,
        }

    async def _maybe_engine_done(
        self, run: AgentRun, turn: int, case_steps: list | None,
        unit_out: dict[str, Any],
    ) -> bool:
        """unit 路径的引擎侧完成判定（与单动作路径同源，不依赖模型自觉 done）。

        条件：用例步骤全部被 journal 覆盖（类型严配），且本单元最后一个成功
        子动作是断言/等待（浏览器已实际验证）。满足则立即完成，省掉后续
        observe→think 往返。
        """
        # 037：验证码断点——头段（含验证码）覆盖完毕 → 尾段固化回放（成功=完成 run）
        #   完成信号：单元提前收束（stopped_early，提交类动作触发）或末动作为断言/等待；
        #   仅 fill 覆盖头段时不得触发（实测：两次 fill 曾误触发，登录未完成即回放必败）
        if (
            getattr(self, "_breakpoint_until", None)
            and self._journal_head_covered()
            and len(self._journal_covered) < len(case_steps or [])
        ):
            _unit_last = str(unit_out.get("last_action") or "")
            if unit_out.get("stopped_early") or _unit_last in ("assert_text", "wait"):
                return await self._on_breakpoint_head_ready(run, turn)
        if not case_steps or len(self._journal_covered) < len(case_steps):
            return False
        if str(unit_out.get("last_action") or "") not in ("assert_text", "wait"):
            return False
        done_msg = (
            f"目标已达成(引擎判定·unit)：{len(case_steps)} 个步骤的轨迹全部覆盖，末步断言已通过"
        )
        logger.info("Bridge: %s (run #%d turn %d)", done_msg, run.id, turn)
        try:
            await crud_agent_run.create_message(
                self.db, run.id, turn, "assistant", done_msg,
            )
        except Exception:
            logger.debug("engine-done 消息写入跳过", exc_info=True)
        await self._complete(run.id, turn)
        return True

    async def _try_compile_first(self, run: AgentRun, agent_id: str, run_id_str: str) -> bool:
        """编译先行（Phase A）：没有可用固化脚本时，先"编译一段脚本"直跑。

        与回放共用一套执行机制（客户端 Playwright + expect 断言）；成功即落库为固化脚本，
        失败/不达标**不记失败轮**，静默回落 OTA 智能执行。
        """
        try:
            from app.config import get_settings

            if not getattr(get_settings(), "compile_first_enabled", False):
                return False
            steps = await self._load_case_steps(run.case_id)
            if not steps or len(steps) < 2:
                return False
            # 036+：含诊断脚本步骤的用例不走编译先行（Playwright 脚本无法忠实覆盖 run_script）
            from core.script_synthesize import case_requires_run_script

            if case_requires_run_script(steps):
                logger.info("Bridge 编译先行：含诊断脚本步骤，跳过 case=%s（走 OTA）", run.case_id)
                return False
            # 037：断点已装载（meta 回放路径）→ 无需重复合成
            if getattr(self, "_breakpoint_until", None):
                return False
            # 037：验证码用例 → 只合成尾段（验证码步骤实时），武装断点后回落 OTA 跑头段
            from core.script_synthesize import case_requires_captcha

            if case_requires_captcha(steps):
                try:
                    from app.config import get_settings as _gs_bp

                    if not bool(getattr(_gs_bp(), "exec_captcha_breakpoint", True)):
                        return False
                except Exception:
                    return False
                return await self._try_compile_first_captcha_tail(run, steps)
            from app.crud import get_test_case

            tc = await get_test_case(self.db, run.case_id)
            base_url = await self._resolve_base_url(run.case_id)

            # 模型/客户端解析：与"跑成功后合成"（_synthesize_compiled_script）**逐字一致**
            from core.llm_wrapper import _resolve_config as _llm_resolve_config
            from core.llm_wrapper import create_openai_client

            llm_client = await create_openai_client(agent_type="execution")
            try:
                _, _, model = await _llm_resolve_config(agent_type="execution")
            except Exception:
                logger.warning("编译先行：模型名解析失败 case=%s", run.case_id, exc_info=True)
                model = ""
            if not (isinstance(model, str) and model.strip()):
                logger.info("编译先行：未解析到模型名 case=%s（模板可编译则仍可直译）", run.case_id)
            from core.script_synthesize import synthesize_script_from_steps

            script = await synthesize_script_from_steps(
                client=llm_client,
                model=model or "",
                case_id=run.case_id,
                case_name=(getattr(tc, "name", None) or "") if tc else "",
                steps=steps,
                base_url=base_url or None,
            )
            if not script:
                logger.info("Bridge 编译先行：未产出可用脚本 case=%s（回落 OTA）", run.case_id)
                return False

            ok = await self._try_replay_compiled_script(
                run, agent_id, run_id_str,
                script_override=script, persist_on_success=True,
            )
            if not ok:
                logger.info(
                    "Bridge 编译先行：脚本执行未通过 case=%s，脚本预览: %s",
                    run.case_id, " / ".join(script.strip().splitlines()[-6:]),
                )
            try:
                await crud_agent_run.create_message(
                    self.db, run.id, 1, "assistant",
                    (
                        f"编译先行：脚本生成并执行成功（{len(script)} 字节，已落库为固化脚本）"
                        if ok else
                        "编译先行：脚本生成并尝试执行未通过，回落智能执行（OTA）"
                    ),
                )
            except Exception:
                logger.debug("编译先行：审计消息写入跳过", exc_info=True)
            return ok
        except Exception:
            logger.warning(
                "Bridge 编译先行失败 case=%s（回落 OTA）", run.case_id, exc_info=True,
            )
            return False

    async def _try_replay_compiled_script(
        self, run: AgentRun, agent_id: str, run_id_str: str, *,
        script_override: str | None = None,
        persist_on_success: bool = False,
    ) -> bool:
        """回放固化脚本；成功即完成 run（返回 True），失败/不支持返回 False 交给 OTA。

        ``script_override``：编译先行产出的脚本（尚未落库）——跳过 hash 校验，
        成功且 ``persist_on_success`` 时落库为正式固化脚本（下次直接回放）。
        """
        from app.crud import get_test_case
        from core.compiled_script import steps_content_hash

        tc = await get_test_case(self.db, run.case_id)
        stored = (getattr(tc, "compiled_script", None) or "").strip() if tc else ""
        script = (script_override or stored).strip()
        if not script:
            return False
        # 037：验证码断点尾段脚本不走常规整案回放（由断点流程：头段实时 → 尾段回放）
        if getattr(self, "_breakpoint_until", None):
            logger.info(
                "Bridge 固化回放：验证码断点模式（尾段脚本），跳过常规回放 case=%s", run.case_id,
            )
            return False
        # 037：验证码用例的"整案脚本"不可信（合成无法真实处理验证码，可能假通过）——
        # 无断点尾段 meta 时一律跳过回放，走 OTA/断点流程（步骤改动会自然触发重合成）
        try:
            from core.script_synthesize import case_requires_captcha

            _steps_for_cap = await self._load_case_steps(run.case_id)
            if case_requires_captcha(_steps_for_cap):
                logger.info(
                    "Bridge 固化回放：验证码用例（无断点尾段 meta），跳过整案回放 case=%s（走 OTA）",
                    run.case_id,
                )
                return False
        except Exception:
            logger.debug("captcha replay guard skipped", exc_info=True)
        steps = await self._load_case_steps(run.case_id)
        if not steps:
            return False
        # 036+：含诊断脚本步骤 → 固化脚本无法忠实回放（防假通过），跳过回放走 OTA
        from core.script_synthesize import case_requires_run_script

        if case_requires_run_script(steps):
            logger.info("Bridge 固化回放：含诊断脚本步骤，跳过 case=%s（走 OTA）", run.case_id)
            return False
        current_hash = steps_content_hash(steps)
        stored_hash = getattr(tc, "compiled_script_hash", None) or ""
        if not script_override and stored_hash and stored_hash != current_hash:
            logger.info(
                "Bridge: compiled_script hash mismatch case=%s stored=%s current=%s — ignore",
                run.case_id, stored_hash[:12], current_hash[:12],
            )
            return False
        from core.script_synthesize import check_script_covers_intents
        missing_cov = check_script_covers_intents(script, steps)
        if missing_cov:
            logger.info(
                "Bridge: compiled_script fails coverage %s case=%s — fallback to OTA",
                missing_cov, run.case_id,
            )
            return False

        base_url = await self._resolve_base_url(run.case_id)
        res = await self.agent_manager._try_run_compiled_script(
            agent_id,
            run_id_str,
            tc.name or f"Case #{run.case_id}",
            steps,
            script=script,
            base_url=base_url,
            case_id=run.case_id,
            steps_hash=stored_hash or current_hash,
            headless=None,
            keep_browser=True,
        )
        ok = (
            bool(res)
            and not any(r.get("compiled_script_failed") for r in res)
            and all(r.get("success") for r in res)
        )
        if not ok:
            logger.info(
                "Bridge: compiled_script replay failed/unsupported case=%s — fallback to OTA",
                run.case_id,
            )
            return False

        logger.info("Bridge: compiled_script replay SUCCESS case=%s steps=%s", run.case_id, len(res))
        # 032 US2：回放窗口 console 证据（客户端随结果回传；失败静默；开关关闭不采集）
        try:
            from app.config import get_settings as _gs_ce

            if bool(getattr(_gs_ce(), "exec_console_evidence", True)):
                self._console_entries = list(
                    getattr(self.agent_manager, "_last_console_errors", []) or []
                )
        except Exception:
            pass
        if persist_on_success and script_override and tc is not None:
            try:
                from core.compiled_script import persist_compiled_script

                persist_compiled_script(tc, script=script, steps_hash=current_hash)
                await self.db.commit()
                logger.info(
                    "Bridge 编译先行：脚本已落库 case=%s bytes=%s（下次直接回放）",
                    run.case_id, len(script),
                )
            except Exception:
                logger.warning(
                    "Bridge 编译先行：脚本落库失败 case=%s", run.case_id, exc_info=True,
                )
        try:
            await crud_agent_run.create_message(
                self.db, run.id, 1, "assistant",
                f"固化脚本回放成功（{len(res)} 步，无 LLM 参与）",
            )
        except Exception:
            logger.debug("Bridge replay message write skipped", exc_info=True)
        self._replay_logs = [
            {
                "step_id": None,
                "level": "info",
                "message": f"固化脚本回放：{r.get('original_description') or ''} — 通过",
                "screenshot_path": None,
            }
            for r in res
        ]
        await self._complete(run.id, 1)
        return True

    # ── 037：验证码断点（头段实时 OTA → 尾段固化回放）────────────────────────

    async def _load_breakpoint_state(self, case_id: int) -> None:
        """装载验证码断点状态（meta + 尾段脚本 + 尾段步骤）。

        装载条件（任一不满足 → 保持未武装，走常规流程）：
        - 开关 ``exec_captcha_breakpoint`` 开
        - 用例含验证码步骤且存在尾段步骤
        - ``compiled_script_meta.captcha_segment_start`` 与脚本/哈希匹配当前步骤
        - 非 run_script 用例（036+ 优先级：含脚本步骤全实时）
        - 尾段脚本覆盖尾段步骤意图（check_script_covers_intents）
        """
        self._breakpoint_until = None
        self._breakpoint_tail_script = None
        self._breakpoint_tail_steps = []
        try:
            from app.config import get_settings

            if not bool(getattr(get_settings(), "exec_captcha_breakpoint", True)):
                return
            from app.crud import get_test_case

            tc = await get_test_case(self.db, case_id)
            if tc is None:
                return
            meta = getattr(tc, "compiled_script_meta", None) or {}
            seg = int(meta.get("captcha_segment_start") or 0) if isinstance(meta, dict) else 0
            script = (getattr(tc, "compiled_script", None) or "").strip()
            if not seg or not script:
                return
            steps = await self._load_case_steps(case_id)
            if not steps:
                return
            from core.script_synthesize import (
                captcha_tail_steps,
                case_requires_run_script,
                check_script_covers_intents,
            )

            if case_requires_run_script(steps):
                return
            from core.compiled_script import steps_content_hash

            if (getattr(tc, "compiled_script_hash", None) or "") != steps_content_hash(steps):
                logger.info("Bridge 断点：hash 不匹配 case=%s — 忽略尾段脚本", case_id)
                return
            tail = captcha_tail_steps(steps)
            if not tail:
                return
            missing = check_script_covers_intents(script, tail)
            if missing:
                logger.info(
                    "Bridge 断点：尾段脚本覆盖不足 %s case=%s — 忽略", missing, case_id,
                )
                return
            self._breakpoint_until = seg
            self._breakpoint_tail_script = script
            self._breakpoint_tail_steps = tail
            logger.info(
                "Bridge 断点已装载 case=%s segment_start=%s tail_steps=%s",
                case_id, seg, len(tail),
            )
        except Exception:
            logger.debug("breakpoint state load failed", exc_info=True)

    def _journal_head_covered(self) -> bool:
        """断点头段（step_order ≤ segment_start）是否已被 journal 全覆盖。"""
        until = getattr(self, "_breakpoint_until", None)
        if not until:
            return False
        head = [o for o in self._ordered_step_numbers() if o <= int(until)]
        return bool(head) and all(o in self._journal_covered for o in head)

    async def _on_breakpoint_head_ready(self, run: AgentRun, turn: int) -> bool:
        """头段覆盖完毕：尝试尾段固化回放。成功=完成 run；失败=清尾段+关断点续 OTA。"""
        if not getattr(self, "_breakpoint_until", None) or not getattr(
            self, "_breakpoint_tail_script", None
        ):
            return False
        logger.info("Bridge 断点：头段覆盖完毕（run #%d turn %d）→ 尾段回放", run.id, turn)
        ok = False
        try:
            ok = await self._replay_breakpoint_tail(run)
        except Exception:
            logger.warning("Bridge 断点尾段回放异常（回落 OTA）", exc_info=True)
        if ok:
            try:
                await crud_agent_run.create_message(
                    self.db, run.id, turn, "assistant",
                    f"验证码断点：头段实时执行完成，尾段固化脚本回放成功"
                    f"（{len(getattr(self, '_breakpoint_replay_logs', []) or [])} 步）",
                )
            except Exception:
                logger.debug("breakpoint success message skipped", exc_info=True)
            await self._complete(run.id, turn)
            return True
        # 尾段回放失败 → 清除坏尾段脚本 + 关闭断点 → 继续 OTA 完成尾段
        await self._clear_breakpoint_artifacts(run.case_id)
        self._breakpoint_until = None
        self._breakpoint_tail_script = None
        try:
            await crud_agent_run.create_message(
                self.db, run.id, turn, "assistant",
                "验证码断点：尾段固化回放未通过，改由智能执行继续完成剩余步骤",
            )
        except Exception:
            logger.debug("breakpoint failure message skipped", exc_info=True)
        return False

    async def _replay_breakpoint_tail(self, run: AgentRun) -> bool:
        """把尾段脚本交给客户端回放（skip_initial_goto：浏览器已停在登录后页面）。"""
        agent_id = getattr(self, "_active_agent_id", None)
        run_id_str = getattr(self, "_active_run_id_str", None)
        if not (agent_id and run_id_str):
            return False
        from app.crud import get_test_case

        tc = await get_test_case(self.db, run.case_id)
        if tc is None:
            return False
        base_url = await self._resolve_base_url(run.case_id)
        # 尾段步骤（回放结果按尾段渲染；头段已由 OTA 覆盖，不得重复计入）
        tail_steps = list(getattr(self, "_breakpoint_tail_steps", []) or [])
        steps = tail_steps or (self._run_steps or await self._load_case_steps(run.case_id))
        res = await self.agent_manager._try_run_compiled_script(
            agent_id,
            run_id_str,
            tc.name or f"Case #{run.case_id}",
            steps,
            script=getattr(self, "_breakpoint_tail_script", None) or "",
            base_url=base_url,
            case_id=run.case_id,
            steps_hash=(getattr(tc, "compiled_script_hash", None) or ""),
            headless=None,
            keep_browser=True,
            skip_initial_goto=True,
        )
        ok = (
            bool(res)
            and not any(r.get("compiled_script_failed") for r in res)
            and all(r.get("success") for r in res)
        )
        if not ok:
            logger.info(
                "Bridge 断点：尾段回放失败 case=%s — 回落 OTA 完成尾段", run.case_id,
            )
            return False
        # 尾段步骤入 journal 覆盖（真实执行过）→ 报告按用例步骤口径渲染
        for s in getattr(self, "_breakpoint_tail_steps", []) or []:
            o = int(s.get("step_order") or 0)
            if o:
                self._journal_covered.add(o)
        self._breakpoint_replay_logs = [
            {
                "step_id": None,
                "level": "info",
                "message": (
                    f"固化脚本回放（验证码断点尾段）："
                    f"{r.get('original_description') or ''} — 通过"
                ),
                "screenshot_path": None,
            }
            for r in res
        ]
        logger.info(
            "Bridge 断点：尾段回放成功 case=%s steps=%s", run.case_id, len(res),
        )
        return True

    async def _clear_breakpoint_artifacts(self, case_id: int) -> None:
        """清除坏尾段脚本（meta+script），防下次执行继续命中。"""
        try:
            from app.crud import get_test_case
            from core.compiled_script import clear_compiled_script

            tc = await get_test_case(self.db, case_id)
            if tc is not None and getattr(tc, "compiled_script_meta", None):
                clear_compiled_script(tc)
                await self.db.commit()
                logger.info("Bridge 断点：坏尾段脚本已清除 case=%s", case_id)
        except Exception:
            logger.debug("breakpoint artifacts clear skipped", exc_info=True)

    async def _load_case_steps(self, case_id: int) -> list[dict]:
        from app import crud as _crud

        rows = await _crud.get_steps_for_case(self.db, case_id)
        return self._steps_to_dicts(rows)

    @staticmethod
    def _steps_to_dicts(rows) -> list[dict]:
        """步骤 dict 形状必须与 client exec / poller 一致，否则 compiled_script hash 对不上。"""
        out: list[dict] = []
        for s in sorted(rows or [], key=lambda x: getattr(x, "step_order", 0) or 0):
            sl = getattr(s, "structured_step", None)
            ll = getattr(s, "learned_locator", None)
            out.append({
                "id": s.id,
                "step_order": s.step_order,
                "description": s.description,
                "expected_result": getattr(s, "parsed_result", ""),
                "learned_locator": ll if isinstance(ll, dict) else None,
                "structured_step": sl if isinstance(sl, dict) else None,
                "cacheable": bool(getattr(s, "cacheable", True)),
            })
        return out

    # ── 断言步骤的确定性覆盖（2026-09-30）─────────────────────────────────
    # 背景：断言类步骤没有"动作"可覆盖，LLM 声明 done 时覆盖门永远不放行 →
    # 实测「refusing premature done: uncovered steps [5]」空转 50 次撞失败轮上限
    # （客户端跑 519 两次都是这样，看起来像"一直不结束"）。这里按快照做确定性验证：
    # **期望文本确实出现在当前快照** → 记为已覆盖（不放水：文本必须真实可见）。
    _ASSERT_STEP_RE = re.compile(r"^\s*(断言|验证|检查|确认)")

    @classmethod
    def _assertion_expectation(cls, description: str) -> str:
        """从断言步骤描述中抽取期望文本：「断言页面包含【X】」→ X。

        与生成器校验器共用 ``core.script_precompile.extract_expected_text``（同一口径）。
        """
        from core.script_precompile import extract_expected_text

        return extract_expected_text(description)

    def _cover_assertion_steps_from_snapshot(
        self, missed: list[int], snapshot: str
    ) -> list[int]:
        """断言步骤：快照中验证到期望文本即记为已覆盖；返回本次覆盖的步骤号。"""
        if not missed or not snapshot:
            return []
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
            if not self._ASSERT_STEP_RE.match(desc):
                continue
            # 期望文本来源：① 描述里带【/「」的原文（最可靠）② 预期结果字段
            # （描述只写"断言排序正确"这类判断句时，用 parsed_result 的字面值）
            candidates: list[str] = []
            extracted = self._assertion_expectation(desc)
            if len(extracted) >= 1:
                candidates.append(extracted)
            expected_result = str(rec.get("parsed_result") or "").strip()
            if len(expected_result) >= 1 and expected_result not in candidates:
                # 预期结果可能带括号包裹，清一遍外层符号
                from core.replay_resolve import clean_target_name

                cleaned = clean_target_name(expected_result) or expected_result
                if len(cleaned) >= 1 and cleaned not in candidates:
                    candidates.append(cleaned)
            matched = False
            for expectation in candidates:
                needle = re.sub(r"\s+", " ", expectation).strip().lower()
                if not needle:
                    continue
                if len(needle) <= 2:
                    # 短期望（如购物车徽标数字「1」）：按**词边界**匹配，
                    # 避免 "1" 命中 "134" 之类的子串造成假通过
                    if re.search(rf"(?<!\w){re.escape(needle)}(?!\w)", hay):
                        matched = True
                        break
                elif needle in hay:
                    matched = True
                    break
            if matched:
                self._journal_covered.add(int(order))
                covered.append(int(order))
        return covered

    def _ordered_step_numbers(self) -> list[int]:
        return [
            int(s.get("step_order") or i + 1)
            for i, s in enumerate(self._run_steps or [])
        ]

    async def _resolve_base_url(self, case_id: int) -> str:
        """环境 base_url 优先，回退项目 base_url（与非 OTA 路径语义一致）。"""
        env_id = getattr(self, "_environment_id", None)
        if env_id:
            try:
                from app.database import AsyncSessionLocal as _ASL
                from app.crud.environment import get_environment as _ge

                async with _ASL() as _edb:
                    _env = await _ge(_edb, env_id)
                    if _env and getattr(_env, "base_url", None):
                        return _env.base_url
            except Exception:
                logger.warning("Bridge: environment %s load failed", env_id, exc_info=True)
        try:
            from app import crud as _crud

            tc = await _crud.get_test_case(self.db, case_id)
            pid = getattr(tc, "project_id", None) if tc else None
            if pid:
                proj = await _crud.get_project(self.db, pid)
                return ((getattr(proj, "base_url", None) or "").strip()) if proj else ""
        except Exception:
            logger.warning("Bridge: project base_url load failed case=%s", case_id, exc_info=True)
        return ""

    def _collect_journal_entry(self, action: dict, turn: int) -> None:
        """成功动作入 journal（含 checklist 映射 + replay 定位符），供 run 结束后合成固化脚本。"""
        name = str(action.get("name") or action.get("action") or "")
        args = action.get("args")
        if not isinstance(args, dict) or not args:
            # unit 子动作把 selector/value 放顶层（无 args 嵌套）。此前丢参数导致
            # 断言步骤无法映射覆盖（case 551 曾因此每轮重复断言、24 轮空转）
            args = {
                key: action.get(key)
                for key in ("selector", "value", "element_desc", "wait_mode")
                if action.get(key) is not None
            }
        entry: dict = {
            "turn": turn,
            "success": True,
            "status": "ok",
            "action": name,
            "selector": args.get("selector"),
            "value": args.get("value"),
            "element_desc": args.get("element_desc"),
            # 034 修复：断言极性覆盖判定需要 wait_mode（text/gone）
            "wait_mode": args.get("wait_mode") or action.get("wait_mode"),
        }
        # 032 US5：坐标动作回解结果入 journal（固化脚本优先语义定位）
        _resolved = args.get("_resolved")
        if isinstance(_resolved, dict) and _resolved:
            entry["resolved"] = _resolved
        idx = pick_checklist_index(self._run_steps, self._journal_covered, entry)
        if idx is None:
            return
        self._journal_covered.add(idx)
        step_rec = next(
            (s for s in self._run_steps if int(s.get("step_order") or 0) == idx), None
        )
        try:
            from core.replay_resolve import build_replay_from_step

            entry["replay"] = build_replay_from_step(
                step_rec,
                action=name,
                selector=args.get("selector"),
                value=args.get("value"),
                dom_attrs=(
                    {"active": _resolved}
                    if isinstance(_resolved, dict) and _resolved else None
                ),
            )
        except Exception:
            logger.debug("Bridge replay record build skipped", exc_info=True)
        entry["checklist_index"] = idx
        entry["checklist_note"] = f"Step {idx}"
        self._journal.append(entry)

    async def _synthesize_compiled_script(self, run: AgentRun, case_id: int) -> None:
        """成功跑完后把 OTA 轨迹固化为可回放脚本（复用 nl_goal 同一套合成+门禁）。"""
        from app.crud import get_test_case
        from core.compiled_script import persist_compiled_script, steps_content_hash
        from core.llm_wrapper import _resolve_config as _llm_resolve_config
        from core.script_synthesize import synthesize_playwright_script

        steps = self._run_steps or await self._load_case_steps(case_id)
        if not steps:
            logger.info("Bridge: synthesis skipped case=%s (no steps loaded)", case_id)
            return
        # 036+：含诊断脚本步骤的用例不固化（Playwright 脚本无法忠实覆盖 run_script）
        from core.script_synthesize import case_requires_run_script

        if case_requires_run_script(steps):
            logger.info("Bridge: synthesis skipped case=%s (contains run_script step)", case_id)
            return
        # 037：验证码用例 → 只固化"尾段"（验证码之后的步骤）+ 断点 meta；
        # 断点开关关闭时不固化（全实时执行）。
        from core.script_synthesize import (
            captcha_breakpoint_index,
            captcha_tail_steps,
            case_requires_captcha,
        )

        if case_requires_captcha(steps):
            try:
                from app.config import get_settings as _gs_bp

                if not bool(getattr(_gs_bp(), "exec_captcha_breakpoint", True)):
                    logger.info(
                        "Bridge: synthesis skipped case=%s (captcha case, breakpoint disabled)",
                        case_id,
                    )
                    return
            except Exception:
                pass
            await self._synthesize_captcha_tail_script(run, case_id, steps)
            return
        tc = await get_test_case(self.db, case_id)
        if tc is None:
            return
        case_name = tc.name or f"Case #{case_id}"
        steps_text = "\n".join(
            f"  {i + 1}. {s.get('description') or ''}" for i, s in enumerate(steps)
        )
        goal_text = f"执行测试用例「{case_name}」\n步骤：\n{steps_text}"
        client = await create_openai_client(agent_type="execution")
        _, _, model = await _llm_resolve_config(agent_type="execution")
        try:
            script = await synthesize_playwright_script(
                client=client,
                model=model,
                case_id=case_id,
                case_name=case_name,
                goal_text=goal_text,
                journal=self._journal,
                steps=steps,
                base_url=self._case_url or None,
            )
        except ValueError as exc:
            logger.warning("Bridge: synthesized script rejected case=%s problems=%s", case_id, exc)
            return
        if not script or not script.strip():
            logger.info("Bridge: synthesis produced empty script case=%s", case_id)
            return
        persist_compiled_script(tc, script=script, steps_hash=steps_content_hash(steps))
        await self.db.commit()
        logger.info(
            "Bridge: synthesized compiled_script case=%s bytes=%s (journal=%s)",
            case_id, len(script), len(self._journal),
        )

    async def _synthesize_captcha_tail_script(self, run: AgentRun, case_id: int, steps: list[dict]) -> None:
        """037：验证码用例的尾段固化（journal 法；尾段=最后一个验证码步骤之后）。

        与整案固化的区别：只固化尾段（验证码步骤本身由实时 solve_captcha 承担），
        脚本带 meta={"captcha_segment_start": N}；回放时 skip_initial_goto（浏览器已登录）。
        """
        from app.crud import get_test_case
        from core.compiled_script import persist_compiled_script, steps_content_hash
        from core.llm_wrapper import _resolve_config as _llm_resolve_config
        from core.script_synthesize import (
            captcha_breakpoint_index,
            captcha_tail_steps,
            synthesize_playwright_script,
        )

        seg = captcha_breakpoint_index(steps)
        tail = captcha_tail_steps(steps)
        if not seg or not tail:
            logger.info("Bridge: captcha tail synthesis skipped case=%s (no tail)", case_id)
            return
        tc = await get_test_case(self.db, case_id)
        if tc is None:
            return
        tail_orders = {int(s.get("step_order") or 0) for s in tail}
        journal_tail = [
            e for e in (self._journal or [])
            if int(e.get("checklist_index") or 0) in tail_orders
        ]
        case_name = tc.name or f"Case #{case_id}"
        steps_text = "\n".join(
            f"  {i + 1}. {s.get('description') or ''}" for i, s in enumerate(tail)
        )
        goal_text = (
            f"执行测试用例「{case_name}」（验证码断点尾段：浏览器已登录并停在当前页面）\n"
            f"步骤：\n{steps_text}"
        )
        client = await create_openai_client(agent_type="execution")
        _, _, model = await _llm_resolve_config(agent_type="execution")
        try:
            script = await synthesize_playwright_script(
                client=client,
                model=model,
                case_id=case_id,
                case_name=case_name,
                goal_text=goal_text,
                journal=journal_tail,
                steps=tail,
                base_url=self._case_url or None,
                no_navigation=True,
            )
        except ValueError as exc:
            logger.warning(
                "Bridge: captcha tail synth rejected case=%s problems=%s", case_id, exc,
            )
            return
        if not script or not script.strip():
            logger.info("Bridge: captcha tail synth produced empty script case=%s", case_id)
            return
        persist_compiled_script(
            tc,
            script=script,
            steps_hash=steps_content_hash(steps),
            meta={"captcha_segment_start": int(seg)},
        )
        await self.db.commit()
        logger.info(
            "Bridge: synthesized captcha tail script case=%s bytes=%s tail_steps=%s (journal=%s)",
            case_id, len(script), len(tail), len(journal_tail),
        )

    async def _try_compile_first_captcha_tail(self, run: AgentRun, steps: list[dict]) -> bool:
        """037 编译先行·验证码尾段：合成尾段脚本 → 落库 + 武装断点（返回 False 继续 OTA 头段）。

        首次执行即可走"头段实时 + 尾段回放"最快路径；合成失败静默回落（跑完 OTA 后
        由 journal 合成兜底）。
        """
        from app.crud import get_test_case
        from core.compiled_script import persist_compiled_script, steps_content_hash
        from core.llm_wrapper import _resolve_config as _llm_resolve_config
        from core.llm_wrapper import create_openai_client
        from core.script_synthesize import (
            captcha_breakpoint_index,
            captcha_tail_steps,
            synthesize_script_from_steps,
        )

        seg = captcha_breakpoint_index(steps)
        tail = captcha_tail_steps(steps)
        if not seg or not tail:
            return False
        tc = await get_test_case(self.db, run.case_id)
        if tc is None:
            return False
        base_url = await self._resolve_base_url(run.case_id)
        llm_client = await create_openai_client(agent_type="execution")
        try:
            _, _, model = await _llm_resolve_config(agent_type="execution")
        except Exception:
            model = ""
        try:
            script = await synthesize_script_from_steps(
                client=llm_client,
                model=model or "",
                case_id=run.case_id,
                case_name=(getattr(tc, "name", None) or ""),
                steps=tail,
                base_url=base_url or None,
                no_navigation=True,
            )
        except Exception:
            logger.warning(
                "Bridge 编译先行（验证码尾段）：合成异常 case=%s", run.case_id, exc_info=True,
            )
            return False
        if not script:
            logger.info(
                "Bridge 编译先行（验证码尾段）：未产出可用脚本 case=%s（回落 OTA）", run.case_id,
            )
            return False
        try:
            persist_compiled_script(
                tc,
                script=script,
                steps_hash=steps_content_hash(steps),
                meta={"captcha_segment_start": int(seg)},
            )
            await self.db.commit()
        except Exception:
            logger.warning(
                "Bridge 编译先行（验证码尾段）：落库失败 case=%s", run.case_id, exc_info=True,
            )
            return False
        # 武装断点：本次头段（含验证码，实时）跑完后由断点流程回放尾段
        self._breakpoint_until = seg
        self._breakpoint_tail_script = script
        self._breakpoint_tail_steps = tail
        logger.info(
            "Bridge 编译先行（验证码尾段）：脚本已落库并武装断点 case=%s segment_start=%s tail=%s",
            run.case_id, seg, len(tail),
        )
        try:
            await crud_agent_run.create_message(
                self.db, run.id, 1, "assistant",
                f"编译先行（验证码断点）：尾段脚本已生成（{len(script)} 字节）；"
                f"头段（含验证码）实时执行，完成后自动回放尾段",
            )
        except Exception:
            logger.debug("compile-first captcha message skipped", exc_info=True)
        return False

    # ── 内部：AgentRun 生命周期 ────────────────────────────────────────────

    async def _create_run(self, case_id: int, goal: dict) -> AgentRun:
        """创建 AgentRun 记录并标记为 running。"""
        ar = AgentRun(
            agent_definition_id=self.agent_def.id,
            case_id=case_id,
            goal=goal,
            status="running",
            started_at=tz_now(),
        )
        self.db.add(ar)
        await self.db.commit()
        await self.db.refresh(ar)

        logger.info(
            "AgentBridge: created AgentRun #%d for case #%d via agent '%s'",
            ar.id, case_id, self.agent_def.name,
        )
        return ar

    async def _complete(self, run_id: int, turns: int) -> None:
        """标记 run 为 completed。"""
        await crud_agent_run.update_agent_run_status(
            self.db, run_id, "completed",
            turns_used=turns,
            completed_at=datetime.now(timezone.utc),
        )
        logger.info("AgentRun #%d completed in %d turns", run_id, turns)

    async def _fail(self, run_id: int, error: str) -> None:
        """标记 run 为 failed；并同步预创建的非终态 TestRun。"""
        # 终止前尽量补一张现场图（导航失败 / 熔断时常没有 turn 截图）
        if (
            self._active_agent_id
            and self._active_run_id_str
            and not self._last_screenshot_path()
        ):
            await self._capture_failure_screenshot(
                self._active_agent_id, self._active_run_id_str, name="failure_final",
            )
        await crud_agent_run.update_agent_run_status(
            self.db, run_id, "failed", error=error,
        )
        # 027: 批量入口会预创建 TestRun(running/pending) 占位，
        # 失败时若不同步置败，报告页将永远显示「运行中/待执行」。
        try:
            from sqlalchemy import text as _text
            row = (await self.db.execute(
                _text("SELECT case_id, (goal->>'batch_id')::int AS bid "
                      "FROM agent_runs WHERE id=:r"),
                {"r": run_id},
            )).first()
            if row and row.case_id and row.bid:
                res = await self.db.execute(
                    _text("UPDATE test_runs SET status='failed' "
                          "WHERE case_id=:c AND batch_id=:b "
                          "AND status IN ('running','pending') "
                          "RETURNING id"),
                    {"c": row.case_id, "b": row.bid},
                )
                tr_ids = [r[0] for r in res.fetchall()]
                # 记录已置败的预创建 TestRun：后续 _save_report 复用同一行，
                # 否则 reuse 查询（pending/running）落空会再建一条重复 run
                # （2026-10-10 实测：客户端批量导航失败 → 1 用例 2 条 run）
                if tr_ids:
                    self._synced_test_run_ids = list(tr_ids)
                # 失败现场截图：取最后一张（最接近终止时刻）
                last_shot = None
                if self._screenshot_paths:
                    last_key = sorted(self._screenshot_paths.keys())[-1]
                    last_shot = self._screenshot_paths[last_key]
                for tr_id in tr_ids:
                    await self.db.execute(
                        _text("INSERT INTO run_logs (run_id, level, message, screenshot_path, timestamp) "
                              "VALUES (:rid, 'error', :msg, :shot, now())"),
                        {"rid": tr_id, "msg": f"执行失败: {error[:180]}", "shot": last_shot},
                    )
                if tr_ids:
                    logger.warning(
                        "已同步置败预创建 TestRun 并写入失败日志 (case=%s batch=%s ×%d)",
                        row.case_id, row.bid, len(tr_ids),
                    )
                await self.db.commit()
        except Exception:
            logger.warning("同步预创建 TestRun 失败", exc_info=True)
        logger.warning("AgentRun #%d failed: %s", run_id, error)

    async def _cancel(
        self, run: "AgentRun", reason: str, *,
        agent_id: str | None = None, run_id_str: str | None = None,
    ) -> None:
        """标记 run 为 cancelled（038 US3：用户停止，不当作失败）。"""
        # 通知客户端停止（best-effort；客户端在两个动作之间检查）
        if agent_id and run_id_str:
            try:
                await self.agent_manager.send(
                    agent_id, {"type": "cancel_run", "run_id": run_id_str},
                )
            except Exception:
                logger.debug("cancel_run 通知客户端失败", exc_info=True)
        await crud_agent_run.update_agent_run_status(
            self.db, run.id, "cancelled", error=reason,
            completed_at=datetime.now(timezone.utc),
        )
        try:
            from sqlalchemy import text as _text

            case_id = getattr(run, "case_id", None)
            batch_id = getattr(self, "_batch_id", None)
            if case_id and batch_id:
                res = await self.db.execute(
                    _text(
                        "UPDATE test_runs SET status='cancelled', end_time=now() "
                        "WHERE case_id=:c AND batch_id=:b "
                        "AND status IN ('running','pending') RETURNING id"
                    ),
                    {"c": case_id, "b": batch_id},
                )
                tr_ids = [r[0] for r in res.fetchall()]
                if tr_ids:
                    self._synced_test_run_ids = list(tr_ids)
                    for tr_id in tr_ids:
                        await self.db.execute(
                            _text(
                                "INSERT INTO run_logs (run_id, level, message, timestamp) "
                                "VALUES (:rid, 'warning', :msg, now())"
                            ),
                            {"rid": tr_id, "msg": "已停止（用户取消）"},
                        )
                await self.db.commit()
        except Exception:
            logger.warning("取消预创建 TestRun 失败", exc_info=True)
        logger.info("AgentRun #%d cancelled: %s", run.id, reason)

    # ── OTA 主循环 ─────────────────────────────────────────────────────────

    async def _ota_loop(
        self, run: AgentRun, agent_id: str, run_id_str: str
    ) -> None:
        """Observe → Think → Act 循环核心。

        每轮：
        1. 向客户端请求页面快照（observe）
        2. 调用 LLM 根据目标 + 历史 + 快照决定下一步（think）
        3. 将 LLM 决策发送到客户端执行（act）
        4. 记录轮次到 agent_messages / agent_tool_calls
        5. 检查目标是否达成
        """
        max_turns = 50
        context_messages: list[dict[str, str]] = []
        llm_client = await create_openai_client()
        consecutive_failures = 0
        retries_used = 0
        successful_act_keys: set = set()
        _last_error_fp: str | None = None
        _consec_err_n = 0
        _decision_fails = 0
        _refuse_done_same = 0
        successful_acts = 0
        assertion_passed = False
        self._active_agent_id = agent_id
        self._active_run_id_str = run_id_str

        # 加载测试用例步骤 + 环境 base_url，作为 LLM 上下文
        case_steps: list[str] = []
        case_url: str = ""
        case_name: str = ""
        if run.case_id:
            try:
                from app import crud as _crud
                _tc = await _crud.get_test_case(self.db, run.case_id)
                if _tc:
                    _steps = await _crud.get_steps_for_case(self.db, run.case_id)
                    for s in _steps:
                        desc = getattr(s, 'description', '') or ''
                        case_steps.append(desc)
                        if '打开' in desc or '导航' in desc or 'http' in desc.lower():
                            import re as _re
                            _m = _re.search(r'https?://\S+', desc)
                            if _m:
                                case_url = _m.group(0)
                    case_name = _tc.name or f"Case #{run.case_id}"
                    self._run_steps = self._steps_to_dicts(_steps)
            except Exception:
                logger.warning("Could not load steps for case %s", run.case_id, exc_info=True)

        # 构建 LLM goal 文本
        steps_text = "\n".join(f"  {i+1}. {s}" for i, s in enumerate(case_steps))
        goal_text = (
            f"执行测试用例「{case_name}」({run.case_id})\n"
            f"必须按以下步骤逐一执行，每步完成后验证结果，\n"
            f"全部步骤完成后返回 done：\n{steps_text}"
        )

        # 加载环境 base_url
        env_base_url: str = ""
        if getattr(self, '_environment_id', None):
            try:
                from app.database import AsyncSessionLocal as _ASL
                async with _ASL() as _edb:
                    from app.crud.environment import get_environment as _ge
                    _env = await _ge(_edb, self._environment_id)
                    if _env and getattr(_env, 'base_url', None):
                        env_base_url = _env.base_url
            except Exception:
                logger.warning("Could not load environment %s", self._environment_id, exc_info=True)

        if env_base_url and not case_url:
            case_url = env_base_url
            logger.info("Bridge: using base_url from environment: %s", env_base_url)

        # 回退项目 base_url（与非 OTA 路径 _resolve_execution_base_url 语义一致）
        if not case_url and run.case_id:
            try:
                from app.database import AsyncSessionLocal as _ASL2
                from app import crud as _crud2
                async with _ASL2() as _pdb:
                    _tc2 = await _crud2.get_test_case(_pdb, run.case_id)
                    _pid = getattr(_tc2, 'project_id', None) if _tc2 else None
                    if _pid:
                        _proj = await _crud2.get_project(_pdb, _pid)
                        _purl = (getattr(_proj, 'base_url', None) or '').strip() if _proj else ''
                        if _purl:
                            case_url = _purl
                            logger.info("Bridge: using base_url from project: %s", _purl)
            except Exception:
                logger.warning("Could not load project base_url for case %s", run.case_id, exc_info=True)

        self._case_url = case_url or ""
        if not case_url:
            logger.warning(
                "Bridge: no base_url resolved (env=%s case=%s) — browser may stay on about:blank",
                getattr(self, '_environment_id', None), run.case_id,
            )

        # 先直接导航到目标 URL（不经过 LLM）
        if case_url:
            logger.info("Bridge: navigating to %s before OTA loop", case_url)
            nav_act = {"name": "browser_navigate", "tool": "browser_navigate",
                       "args": {"selector": None, "value": case_url}}
            try:
                nav_result = await asyncio.wait_for(
                    self.agent_manager.send_act(agent_id, run_id_str, nav_act),
                    timeout=30,
                )
                self._save_screenshot(nav_result.get("screenshot_b64"), "nav_initial")
                if nav_result.get("success"):
                    logger.info("Bridge: navigation successful")
                    # 初始导航计入 checklist（打开步骤），否则 LLM 易空口 done
                    self._collect_journal_entry(nav_act, turn=0)
                else:
                    reason = nav_result.get("error") or "unknown"
                    msg = f"导航失败，已停止执行: {case_url} — {reason}"
                    logger.error("Bridge: %s", msg)
                    # 导航失败时常无 screenshot_b64（DNS 错误等）——强制再截一帧挂报告
                    if not self._screenshot_paths.get("nav_initial"):
                        await self._capture_failure_screenshot(
                            agent_id, run_id_str, name="nav_fail",
                        )
                    await self._fail(run.id, msg)
                    return
            except Exception as e:
                msg = f"导航失败，已停止执行: {case_url} — {e}"
                logger.error("Bridge: %s", msg)
                await self._capture_failure_screenshot(
                    agent_id, run_id_str, name="nav_fail",
                )
                await self._fail(run.id, msg)
                return

        # 上轮动作后验证快照：轮次之间无其他页面操作，直接复用。
        carried_obs: dict | None = None
        max_failed_turns = max_turns
        max_total_turns = max(200, max_turns * 4)
        turn = 0
        failed_turns = 0
        while turn < max_total_turns:
            # 038 US3：每轮开始前检查停止（跨 worker 走 DB 轮询，同进程走内存标志）
            if await self._stop_watcher.should_stop():
                await self._cancel(
                    run, "已停止（用户取消）", agent_id=agent_id, run_id_str=run_id_str,
                )
                return
            if failed_turns >= max_failed_turns:
                await self._fail(
                    run.id,
                    f"失败轮达到上限 ({failed_turns}/{max_failed_turns})；"
                    f"总轮次 {turn}",
                )
                return
            turn += 1
            _turn_t0 = time.monotonic()
            logger.info(
                "━━━ Turn %d/%d (总 %d/%d) (run #%d) ━━━",
                failed_turns + 1, max_failed_turns, turn, max_total_turns, run.id,
            )

            # ── 1. Observe: WS 取快照（037：exec_vision 开启时每轮附截图；失败轮/困难页兜底）──
            if carried_obs is not None:
                obs = carried_obs
                carried_obs = None
                logger.debug("Bridge reusing post-act snapshot (turn %d)", turn)
            else:
                want_shot = failed_turns > 0 or self._vision_on()
                try:
                    obs = await asyncio.wait_for(
                        self.agent_manager.send_observe(
                            agent_id, run_id_str, want_screenshot=want_shot,
                        ),
                        timeout=60,
                    )
                except asyncio.TimeoutError:
                    logger.error("Observe timeout at turn %d", turn)
                    await self._fail(run.id, f"Observe timeout at turn {turn}")
                    return
                except Exception as exc:
                    logger.exception("Observe error at turn %d", turn)
                    await self._fail(run.id, f"Observe error at turn {turn}: {exc}")
                    return

            if not obs.get("success"):
                err = obs.get("error", "unknown observe failure")
                logger.error("Observe failed at turn %d: %s", turn, err)
                consecutive_failures += 1
                failed_turns += 1
                if consecutive_failures >= 3:
                    await self._fail(run.id, f"Observe failed {consecutive_failures} times: {err}")
                    return
                continue

            consecutive_failures = 0
            snapshot = obs.get("snapshot", "")
            page_url = obs.get("page_url", "")

            # 困难页且尚未带图：补一次截图 observe
            if (
                not (obs.get("screenshot_b64") or "").strip()
                and should_capture_screenshot(snapshot, consecutive_failures=failed_turns)
            ):
                try:
                    obs_shot = await asyncio.wait_for(
                        self.agent_manager.send_observe(
                            agent_id, run_id_str, want_screenshot=True,
                        ),
                        timeout=60,
                    )
                    if obs_shot.get("success") and obs_shot.get("screenshot_b64"):
                        obs = obs_shot
                        snapshot = obs.get("snapshot", "") or snapshot
                        page_url = obs.get("page_url", "") or page_url
                except Exception:
                    logger.debug("Bridge supplemental screenshot observe skipped", exc_info=True)

            # 保存观察截图
            self._save_screenshot(obs.get("screenshot_b64"), f"turn_{turn:02d}_observe")

            # 关闭消息步骤：快照有可见关闭候选时，必须先真实点击
            from core.goal_agent_loop import is_close_messages_checklist_step
            from core.step_intent import close_control_candidates
            _has_close_step = any(
                is_close_messages_checklist_step(s) for s in (case_steps or [])
            )
            _close_refs = [
                c.get("ref")
                for c in close_control_candidates(snapshot or "")
                if (c.get("ref") or "").strip()
            ]
            logger.info(
                "Bridge turn %d: observe %.1fs（快照 %d 字符）",
                turn, time.monotonic() - _turn_t0, len(snapshot or ""),
            )

            # ── Phase B(auto)：确定性自动成组（机械段一次往返执行，不用 LLM）──
            _auto_actions = self._auto_unit_actions(snapshot)
            if _auto_actions:
                _auto_out = await self._execute_action_unit(
                    run, turn, agent_id, run_id_str, _auto_actions,
                    snapshot, context_messages,
                )
                if _auto_out.get("success"):
                    if await self._maybe_engine_done(run, turn, case_steps, _auto_out):
                        return
                    continue
                self._auto_unit_chain_ok = False
                try:
                    await crud_agent_run.create_message(
                        self.db, run.id, turn, "assistant",
                        f"自动成组未完成（已完成 {_auto_out.get('executed', 0)} 步），转 LLM 决策",
                    )
                except Exception:
                    logger.debug("unit(auto) 审计消息写入跳过", exc_info=True)
                continue

            # ── 2. Think: LLM 决策 ──
            _think_t0 = time.monotonic()
            action = await self._llm_decide(
                llm_client=llm_client,
                goal=goal_text,
                snapshot=snapshot,
                page_url=page_url,
                context_messages=context_messages,
                turn=turn,
                case_steps=case_steps,
                case_url=case_url,
                close_refs=_close_refs if _has_close_step else (),
                screenshot_b64=obs.get("screenshot_b64") or None,
            )
            logger.info(
                "Bridge turn %d: think %.1fs", turn, time.monotonic() - _think_t0,
            )

            if action is None:
                _decision_fails += 1
                failed_turns += 1
                logger.warning(
                    "LLM decision returned None at turn %d (consecutive=%d)",
                    turn, _decision_fails,
                )
                if _decision_fails >= 4:
                    await self._fail(
                        run.id,
                        f"LLM 连续 {_decision_fails} 轮未返回合法工具调用 JSON",
                    )
                    return
                context_messages.append({
                    "role": "user",
                    "content": (
                        "SYSTEM: 你上一条回复不是合法 JSON，已被丢弃。"
                        "只输出一个 JSON 对象（字段：thinking/action/selector/"
                        "element_desc/value），不要输出任何解释性文字。"
                    ),
                })
                continue
            _decision_fails = 0

            # 检查停止信号
            if action.get("_done"):
                # 037：验证码断点——头段覆盖完毕时的 done 走断点完成路径
                if (
                    getattr(self, "_breakpoint_until", None)
                    and self._journal_head_covered()
                    and len(self._journal_covered) < len(case_steps or [])
                ):
                    if await self._on_breakpoint_head_ready(run, turn):
                        return
                    # 尾段回放失败 → 断点已关闭，落入下方门禁（按全量步骤拒绝该 done）
                if _has_close_step and _close_refs:
                    logger.info(
                        "Bridge refusing premature done: %d close controls "
                        "still visible (run #%d turn %d)",
                        len(_close_refs), run.id, turn,
                    )
                    context_messages.append({
                        "role": "assistant",
                        "content": (
                            "SYSTEM: 页面仍有可见关闭控件，CLOSE_MESSAGES "
                            "步骤未完成，不得 done，继续关闭。"
                        ),
                    })
                    continue
                # 引擎门禁：有用例步骤时必须 journal 全覆盖，禁止 LLM 空口 done
                if case_steps and len(self._journal_covered) < len(case_steps):
                    _missed = [
                        o for o in self._ordered_step_numbers()
                        if o not in self._journal_covered
                    ]
                    # ① 断言步骤：按快照做确定性验证，验证到即覆盖（不再空转）
                    _auto = self._cover_assertion_steps_from_snapshot(_missed, snapshot)
                    if _auto:
                        logger.info(
                            "Bridge auto-covered assertion steps %s from snapshot "
                            "(run #%d turn %d)",
                            _auto, run.id, turn,
                        )
                        _missed = [
                            o for o in self._ordered_step_numbers()
                            if o not in self._journal_covered
                        ]
                    if _missed:
                        # ② 同因拒绝上限：禁止"拒绝 done"无限空转（曾烧满 50 失败轮）
                        _refuse_done_same += 1
                        if _refuse_done_same >= _REFUSE_DONE_LIMIT:
                            await self._fail(
                                run.id,
                                f"剩余步骤无法完成：{_missed}"
                                f"（连续 {_refuse_done_same} 轮 done 被拒且无进展）",
                            )
                            return
                        failed_turns += 1
                        logger.info(
                            "Bridge refusing premature done: uncovered steps %s "
                            "(covered %s/%s, run #%d turn %d, refuse=%d)",
                            _missed, len(self._journal_covered), len(case_steps),
                            run.id, turn, _refuse_done_same,
                        )
                        context_messages.append({
                            "role": "assistant",
                            "content": (
                                "SYSTEM: 仍有未完成的测试步骤 "
                                f"{_missed}，不得返回 done。"
                                "必须用工具真正执行剩余步骤；"
                                "若无法完成请返回 action=error。"
                            ),
                        })
                        continue
                    # 全部步骤已覆盖（含断言自动覆盖）→ 放行到完成路径
                _summary = action.get("_summary") or ""
                await crud_agent_run.create_message(
                    self.db, run.id, turn, "assistant",
                    f"目标已达成: {_summary}" if _summary else "目标已达成",
                )
                await self._complete(run.id, turn)
                return

            if action.get("_error"):
                # Agent reported error — 双重熔断（027-e2e-fixes 迭代）：
                #   同因(指纹相同)第 2 次 / 连续任意 error 第 3 次 → 终止
                _err_msg = str(action.get("_error_message", ""))
                failed_turns += 1
                from core.mcp_args import error_fingerprint
                _fp = error_fingerprint(_err_msg)
                same_as_last = bool(_fp) and _fp == _last_error_fp
                _consec_err_n += 1
                if (same_as_last and _consec_err_n >= 2) or _consec_err_n >= 3:
                    reason = ("连续同因错误" if same_as_last else "连续错误")
                    await self._fail(
                        run.id,
                        f"{reason}(×{_consec_err_n}): {_err_msg[:120]}",
                    )
                    return
                _last_error_fp = _fp
                logger.warning(
                    "Agent reported error at turn %d (%d/3%s): %s",
                    turn, _consec_err_n,
                    " 同因" if same_as_last else "", _err_msg[:100],
                )
                continue

            # 032 US3：失败时客户端优先元素级截图（开关；send 时浅拷贝注入，不污染记录）
            try:
                from app.config import get_settings as _gs_el
                _el_ss = bool(getattr(_gs_el(), "exec_element_screenshot", True))
            except Exception:
                _el_ss = True

            # ── Phase B：宏动作单元（一次往返推进一段机械动作）──
            if isinstance(action.get("unit"), list):
                _unit = await self._execute_action_unit(
                    run, turn, agent_id, run_id_str, action.get("unit"),
                    snapshot, context_messages,
                )
                if _unit.get("success"):
                    if await self._maybe_engine_done(run, turn, case_steps, _unit):
                        return
                    continue  # 已逐步入账；下一轮 observe → think
                failed_turns += 1
                if failed_turns >= max_failed_turns:
                    await self._fail(
                        run.id,
                        f"失败轮达到上限 ({failed_turns}/{max_failed_turns})；"
                        f"宏动作单元步进失败: {str(_unit.get('error'))[:80]}",
                    )
                    return
                continue

            # ── 3. Act: WS 执行 ──
            try:
                _act_t0 = time.monotonic()
                _act_kind = str(action.get("name", action.get("action", "")) or "").lower()
                if _act_kind == "run_script":
                    # 036+：执行链诊断脚本（服务端沙箱执行，不经客户端；结果文本回喂模型）
                    result = await self._execute_script_action(action)
                elif _act_kind == "solve_captcha":
                    # 037：执行链验证码（模型读图识别；拆解为既有 evaluate/fill 基础动作 → 旧客户端可用）
                    result = await self._execute_captcha_action(agent_id, run_id_str, action)
                elif _act_kind == "fetch":
                    # 036：fetch 由 bridge 驱动（既有 evaluate 动作序列 + 分块回传；
                    # 证据字节在服务端落盘——客户端无需新增动作）
                    result = await self._execute_fetch_action(agent_id, run_id_str, action)
                else:
                    result = await asyncio.wait_for(
                        self.agent_manager.send_act(agent_id, run_id_str, {**action, "_element_screenshot": _el_ss}),
                        timeout=120,
                    )
            except asyncio.TimeoutError:
                logger.warning("Act timeout at turn %d", turn)
                result = {"success": False, "error": "Act timeout (120s)"}
            except Exception as exc:
                logger.exception("Act error at turn %d", turn)
                result = {"success": False, "error": str(exc)}
            logger.info(
                "Bridge turn %d: act %.1fs", turn, time.monotonic() - _act_t0,
            )

            # 032：副作用动作 transport 级失败 → "可能已生效"，豁免本地重试与回退
            _act_label = str(action.get("name", action.get("action", "")) or "")
            if annotate_uncertain(_act_label, result):
                logger.warning(
                    "Bridge: act uncertain（可能已生效，跳过本地重试/回退）turn %d: %s",
                    turn, str(result.get("error"))[:100],
                )

            # Phase C：瞬时失败（元素未就绪/超时/被替换）本地重试，不计失败轮
            if not result.get("success") and not result.get("_uncertain"):
                from core.mcp_args import is_transient_action_error

                if is_transient_action_error(result.get("error")):
                    for _delay in (0.6, 1.5):
                        logger.info(
                            "Bridge: act 瞬时失败，本地重试（%.1fs 后）: %s",
                            _delay, str(result.get("error"))[:80],
                        )
                        await asyncio.sleep(_delay)
                        try:
                            result = await asyncio.wait_for(
                                self.agent_manager.send_act(agent_id, run_id_str, {**action, "_element_screenshot": _el_ss}),
                                timeout=120,
                            )
                        except asyncio.TimeoutError:
                            result = {"success": False, "error": "Act timeout (120s)"}
                        except Exception as exc:  # noqa: BLE001
                            result = {"success": False, "error": str(exc)}
                        if result.get("success"):
                            break

            # ── 3b. Act 失败恢复：Cursor click_xy 兜底 + stale ref 刷新 ──
            # 032：uncertain 时跳过恢复重试（避免重复执行副作用动作）
            if not result.get("success") and not result.get("_uncertain"):
                error = result.get("error", "") or ""
                from core.locator_candidates import (
                    actionable_candidates,
                    extract_candidates,
                    is_snapshot_ref,
                )
                from core.self_healing import build_failure_hint, is_stale_ref_error

                # bbox→click_xy once when ref click misses
                _act_name = str(action.get("name", action.get("action", "")) or "").lower()
                _sel = (action.get("args") or {}).get("selector")
                if _act_name in ("click", "double_click", "right_click", "check", "hover") and is_snapshot_ref(_sel):
                    try:
                        cands = actionable_candidates(extract_candidates(snapshot or ""))
                        cand = find_candidate(cands, _sel)
                        role = getattr(cand, "role", None) if cand else "button"
                        name = getattr(cand, "name", None) if cand else (
                            (action.get("args") or {}).get("element_desc")
                        )
                        eval_act = bridge_evaluate_act(role, name)
                        if eval_act is not None:
                            bbox_res = await asyncio.wait_for(
                                self.agent_manager.send_act(agent_id, run_id_str, eval_act),
                                timeout=30,
                            )
                            center = parse_bbox_center_from_text(
                                (bbox_res or {}).get("text")
                                or (bbox_res or {}).get("error")
                                or ""
                            )
                            if center is None and isinstance(bbox_res, dict):
                                # some clients put evaluate return in message/result
                                center = parse_bbox_center_from_text(
                                    str(bbox_res.get("result") or bbox_res.get("value") or "")
                                )
                            if center is not None:
                                xy_act = bridge_click_xy_act(
                                    center[0], center[1],
                                    thinking=f"ref click failed; click_xy for {_sel}",
                                )
                                result = await asyncio.wait_for(
                                    self.agent_manager.send_act(agent_id, run_id_str, xy_act),
                                    timeout=120,
                                )
                                if result.get("success"):
                                    action = xy_act
                                logger.info(
                                    "Bridge click_xy fallback (turn %d): %s success=%s",
                                    turn, center, result.get("success"),
                                )
                    except Exception as exc:
                        logger.warning(
                            "Bridge click_xy fallback failed (turn %d): %s", turn, exc,
                        )

                if not result.get("success") and retries_used < 1 and is_stale_ref_error(error):
                    retries_used += 1
                    try:
                        obs_retry = await asyncio.wait_for(
                            self.agent_manager.send_observe(
                                agent_id, run_id_str, want_screenshot=True,
                            ),
                            timeout=60,
                        )
                        retry_action = await self._llm_decide(
                            llm_client=llm_client,
                            goal=goal_text,
                            snapshot=obs_retry.get("snapshot", ""),
                            page_url=obs_retry.get("page_url", ""),
                            context_messages=context_messages,
                            turn=turn,
                            case_steps=case_steps,
                            case_url=case_url,
                            extra_hint=build_failure_hint(error),
                            screenshot_b64=obs_retry.get("screenshot_b64") or None,
                        )
                        if (
                            isinstance(retry_action, dict)
                            and not retry_action.get("_done")
                            and not retry_action.get("_error")
                        ):
                            action = retry_action
                            if isinstance(action.get("unit"), list):
                                # 036 修复：恢复路径不得绕过宏动作单元
                                # （此前直接 send_act(unit) → 客户端「未知操作」）
                                result = await self._execute_action_unit(
                                    run, turn, agent_id, run_id_str,
                                    action.get("unit"),
                                    obs_retry.get("snapshot", ""),
                                    context_messages,
                                )
                            else:
                                result = await asyncio.wait_for(
                                    self.agent_manager.send_act(agent_id, run_id_str, {**action, "_element_screenshot": _el_ss}),
                                    timeout=120,
                                )
                            logger.info(
                                "Bridge stale-ref recovery (turn %d): success=%s",
                                turn, result.get("success"),
                            )
                    except Exception as exc:
                        logger.warning(
                            "Bridge stale-ref recovery failed (turn %d): %s", turn, exc,
                        )

            # 032 US6/US7：失败补充证据（find 辅助 + 隐藏元素诊断；均静默降级、不改变成败）
            if not result.get("success") and not result.get("_uncertain"):
                await self._enrich_failure_evidence(agent_id, run_id_str, action, result, snapshot)

            # 动作后确定性验证（028：与 nl_goal 同一 verifier，MCP 成功不等于页面已变化）
            _ver_name = str(action.get("name", action.get("action", "")) or "").lower().replace("browser_", "")
            if result.get("success") and _ver_name in ("click", "press", "hover"):
                try:
                    from core.locator_verification import LocatorActionEvidence, verify_locator_action

                    _after_obs = await asyncio.wait_for(
                        self.agent_manager.send_observe(agent_id, run_id_str),
                        timeout=60,
                    )
                    _after_snapshot = _after_obs.get("snapshot", "") if isinstance(_after_obs, dict) else ""
                    _verification = verify_locator_action(
                        LocatorActionEvidence(
                            action=_ver_name,
                            before_snapshot=snapshot or "",
                            after_snapshot=_after_snapshot or "",
                        )
                    )
                    result["verification"] = {
                        "verified": _verification.verified,
                        "failure_kind": _verification.failure_kind,
                    }
                    if _verification.verified and isinstance(_after_obs, dict):
                        _after_snap = _after_obs.get("snapshot", "") or ""
                        if _after_snap and "(snapshot unavailable)" not in _after_snap:
                            carried_obs = {
                                "success": True,
                                "snapshot": _after_snap,
                                "page_url": _after_obs.get("page_url", page_url),
                            }
                    if not _verification.verified:
                        result["success"] = False
                        result["error"] = _verification.failure_kind
                except Exception:
                    logger.debug("Bridge post-act verification observe skipped", exc_info=True)

            # 保存操作截图
            self._save_screenshot(result.get("screenshot_b64"), f"turn_{turn:02d}_act")

            # ── 4. 记录轮次 ──
            # 032 US5：坐标动作成功后回解命中元素（语义信息入账；失败静默）
            if result.get("success"):
                await self._resolve_coordinate_target(agent_id, run_id_str, action, result)
                await self._append_coord_feedback(
                    agent_id, run_id_str, action, result, context_messages,
                )

            await self._record_turn(run.id, turn, snapshot, action, result)

            # 037 US3：失败现场视觉自诊断——本帧无截图时补拍（元素级优先），
            # b64 缓存后由下一轮 _llm_decide 回喂（有新鲜帧时不消费）
            if not result.get("success") and not (obs.get("screenshot_b64") or "").strip():
                try:
                    _sel = action.get("selector")
                    await self._capture_failure_screenshot(
                        agent_id, run_id_str,
                        name=f"fail_turn_{turn:02d}",
                        target=(
                            str(_sel)
                            if _sel and not str(_sel).startswith(("text=", "css="))
                            else None
                        ),
                    )
                except Exception:
                    logger.debug("failure vision capture skipped", exc_info=True)

            # ── 5. 更新上下文 ──
            action_name = action.get("name", action.get("action", "?"))
            context_messages.append({
                "role": "assistant",
                "content": f"{action_name}: {action.get('args', {})}",
            })
            from core.mcp_args import build_tool_status
            status_text = build_tool_status(result)
            context_messages.append({"role": "tool", "content": status_text})
            if result.get("_uncertain"):
                _hint = uncertain_context_hint(str(action_name))
                context_messages.append({"role": "user", "content": _hint})
                try:
                    await crud_agent_run.create_message(
                        self.db, run.id, turn, "user", _hint,
                    )
                except Exception:
                    logger.debug("uncertain 提示消息写入跳过", exc_info=True)

            from core.mcp_args import act_count_key
            _act_key = act_count_key(action)
            if result.get("success") and _act_key is not None and _act_key not in successful_act_keys:
                successful_act_keys.add(_act_key)
            if not result.get("success"):
                failed_turns += 1
            successful_acts = len(successful_act_keys)
            # 成功操作打断 error 连击（027 熔断重置点）
            if result.get("success"):
                _consec_err_n = 0
                _last_error_fp = None
                self._collect_journal_entry(action, turn)
                self._auto_unit_chain_ok = True  # LLM 推进成功 → 允许再次自动成组
                _refuse_done_same = 0  # 有真实进展 → 重置「拒绝 done」计数
            if "断言通过" in status_text:
                assertion_passed = True

            # ── 6. 引擎侧完成判定（025-ref-click：不依赖模型自觉）──
            # 条件一：最后动作是断言且成功，且成功操作数 ≥ 步骤数
            # 条件二：本轮已出现过断言成功，且成功操作数 ≥ 步骤数
            #        （防模型无视证据重复执行步骤）
            _all_steps_covered = (
                bool(case_steps) and len(self._journal_covered) >= len(case_steps)
            )
            engine_done = _all_steps_covered and successful_acts >= len(case_steps) and (
                (result.get("success") and action_name in ("assert_text", "wait"))
                or assertion_passed
            )
            if engine_done:
                done_msg = (
                    f"目标已达成(引擎判定): {len(case_steps)} 个步骤的工具结果全部成功，"
                    f"最终断言「{action.get('args', {}).get('value', '')}」已通过"
                )
                logger.info("Bridge: %s (run #%d)", done_msg, run.id)
                await crud_agent_run.create_message(
                    self.db, run.id, turn, "assistant", done_msg,
                )
                await self._complete(run.id, turn)
                return

        _missed = [
            o for o in self._ordered_step_numbers() if o not in self._journal_covered
        ]
        await self._fail(
            run.id,
            f"总轮次达到上限 ({max_total_turns})；失败轮 {failed_turns}/{max_failed_turns}；"
            f"未覆盖步骤: {_missed or 'none'}",
        )

    # ── LLM 决策 ────────────────────────────────────────────────────────────

    async def _llm_decide(
        self,
        llm_client,
        goal: str,
        snapshot: str,
        page_url: str,
        context_messages: list[dict[str, str]],
        turn: int,
        case_steps: list[str] | None = None,
        case_url: str = "",
        extra_hint: str = "",
        close_refs: tuple = (),
        screenshot_b64: str | None = None,
    ) -> dict[str, Any] | None:
        """LLM 决策：Cursor 契约 + 可选截图视觉。

        Returns:
            dict with "name"/"args" keys for normal actions,
            dict with "unit" for a batch unit (mechanical sub-actions),
            dict with "_done": True for goal-completion signal,
            dict with "_error": True for agent error signal,
            None if LLM call failed (skip turn).
        """
        # Cursor 动作契约为主；Agent 角色上下文仅作补充，不得冲掉动作表
        role_ctx = await resolve_prompt_for_agent(
            self.db, "execution", "step_execute",
            variables={"goal": goal, "snapshot": snapshot},
        )
        system_prompt = OTA_CURSOR_SYSTEM_PROMPT
        if (role_ctx or "").strip() and OTA_CURSOR_SYSTEM_PROMPT.strip() not in role_ctx:
            system_prompt = (
                f"{OTA_CURSOR_SYSTEM_PROMPT}\n\n"
                f"--- AGENT ROLE CONTEXT ---\n{role_ctx.strip()}\n"
                "REMINDER: the Cursor action schema above is AUTHORITATIVE."
            )

        # 构建上下文历史文本（最近 5 轮 = 10 条消息）
        context_text = ""
        for msg in context_messages[-10:]:
            context_text += f"[{msg['role']}] {msg['content']}\n"

        from core.locator_candidates import (
            actionable_candidates,
            extract_candidates,
            is_snapshot_ref,
            serialize_candidates,
            snapshot_version,
            validate_candidate_ref,
        )
        current_snapshot_version = snapshot_version(snapshot)
        candidates = actionable_candidates(extract_candidates(snapshot))
        steps_text = ""
        if case_steps:
            steps_text = "\n".join(
                f"  Step {i+1}: {s}" for i, s in enumerate(case_steps)
            )
        nav_instruction = ""
        if case_url and (not page_url or "about:blank" in page_url):
            nav_instruction = (
                f"\n第一步操作：使用 goto / browser_navigate 打开 {case_url}"
            )

        # 037 US3：无新帧时回喂失败现场截图（一次性消费；有新帧则作废陈旧失败帧）
        if screenshot_b64:
            self._pending_failure_shot_b64 = None
        elif getattr(self, "_pending_failure_shot_b64", None):
            screenshot_b64 = self._pending_failure_shot_b64
            self._pending_failure_shot_b64 = None

        # 037：视觉帧压缩（PNG b64 → JPEG data URL；控 token；异常时原样兜底）
        if screenshot_b64:
            logger.info(
                "Bridge turn %d: vision frame attached (%d bytes b64)", turn, len(screenshot_b64),
            )
            try:
                from core.vision_frame import b64_to_frame_data_url

                try:
                    from app.config import get_settings

                    _max_w = int(getattr(get_settings(), "exec_vision_max_width", 1152) or 1152)
                except Exception:
                    _max_w = 1152
                screenshot_b64 = (
                    b64_to_frame_data_url(screenshot_b64, max_width=_max_w) or screenshot_b64
                )
            except Exception:
                logger.debug("vision frame compress skipped", exc_info=True)

        step_description = (
            f"GOAL: {goal}\n\n"
            f"TEST CASE STEPS (complete ALL of them; when consecutive steps are mechanical — you already know the target and value — group them into ONE 'unit' array instead of one action per turn):\n{steps_text}\n\n"
            f"HISTORY (recent actions and results):\n{context_text}\n"
            f"CURRENT URL: {page_url}\n"
            f"{nav_instruction}\n\n"
            f"IMPORTANT: You MUST complete ALL test steps above before returning done. "
            f"Only return done after verifying the last step's expected result. "
            f"Based on the PAGE CONTENT below, decide the NEXT ACTION — either one action, "
            f"or a 'unit' array of up to 6 MECHANICAL sub-actions (fill/click/select/"
            f"press_key/wait/assert_text with optional fallbacks) — to move towards the GOAL. "
            f"If stuck or impossible, use action='error' with explanation in value."
            f"\n\nSNAPSHOT VERSION: {current_snapshot_version}"
            f"\nCANDIDATE ELEMENTS (choose selector only from this list):\n"
            f"{serialize_candidates(candidates) or '(none)'}"
        )
        step_description += (
            f"\n\nHARD RULES: "
            f"(a) NEVER repeat an action whose tool result was successful. "
            f"(a2) When the next 2-6 checklist steps need no new decision between them "
            f"(e.g. fill several fields -> click -> select -> assert), emit ONE \"unit\" array "
            f"covering them instead of dripping one action per turn; include \"fallbacks\" "
            f"(CSS/text=) for each sub-action. "
            f"(b) Prefer snapshot refs; use click_xy when a screenshot is attached "
            f"and refs are unreliable (overlay/canvas). "
            f"(c) When HISTORY shows every test-case step has a successful tool result, "
            f"you MUST immediately return done. No exceptions."
        )
        if screenshot_b64:
            step_description += (
                "\n\nA VIEWPORT SCREENSHOT is attached for this turn."
            )
        if extra_hint:
            step_description += f"\n\nRETRY CONTEXT: {extra_hint}"
        if close_refs:
            step_description += (
                f"\n\nCLOSE_MESSAGES: 快照中有 {len(close_refs)} 个可见关闭控件，"
                f"下一动作必须是 click {close_refs[0]}（CLOSE_MESSAGES 步骤）。"
                f"不得点击消息铃铛/去查看，不得返回 done。"
            )

        try:
            tool_call = await generate_tool_call(
                step_description=step_description,
                dom_snapshot=snapshot,
                client=llm_client,
                system_prompt=system_prompt,
                screenshot_b64=screenshot_b64,
                replace_system_prompt=True,
            )
        except Exception as exc:
            if screenshot_b64:
                # 037：网关/模型不支持图片 → 纯文本重试一次并关闭本 run 视觉（探索链同口径）
                logger.warning(
                    "Vision decision failed at turn %d, retry text-only: %s", turn, str(exc)[:200]
                )
                self._vision_off = True
                try:
                    tool_call = await generate_tool_call(
                        step_description=step_description.replace(
                            "\n\nA VIEWPORT SCREENSHOT is attached for this turn.", ""
                        ),
                        dom_snapshot=snapshot,
                        client=llm_client,
                        system_prompt=system_prompt,
                        screenshot_b64=None,
                        replace_system_prompt=True,
                    )
                except Exception:
                    logger.exception("LLM decision failed at turn %d", turn)
                    return None
            else:
                logger.exception("LLM decision failed at turn %d", turn)
                return None

        tc_dict = tool_call.model_dump()
        action_name = tc_dict.get("action", "")
        selected_ref = tc_dict.get("selector")
        act_l = (action_name or "").strip().lower()
        if is_snapshot_ref(selected_ref) and act_l not in (
            "click_xy", "browser_mouse_click_xy", "move_mouse", "drag_xy",
        ):
            validation = validate_candidate_ref(
                selected_ref,
                snapshot_version=current_snapshot_version,
                decision_version=current_snapshot_version,
                candidates=candidates,
            )
            if not validation.valid:
                logger.warning(
                    "AgentBridge rejected locator ref=%s kind=%s",
                    selected_ref,
                    validation.failure_kind,
                )
                return {
                    "_error": True,
                    "_error_message": f"locator rejected: {validation.failure_kind}",
                }

        # click_xy without numeric coords but with a ref → treat as normal click
        if act_l in ("click_xy", "browser_mouse_click_xy") and is_snapshot_ref(selected_ref):
            import re as _re
            nums = [int(n) for n in _re.findall(r"-?\d+", tc_dict.get("value") or "")]
            if len(nums) < 2:
                action_name = "click"
                act_l = "click"
                tc_dict["action"] = "click"

        # 停止信号：done / error
        if action_name == "done":
            summary = tc_dict.get("value") or ""
            logger.info("Agent declared goal achieved: %s", summary)
            return {"_done": True, "_summary": summary}

        if action_name == "error":
            err_msg = tc_dict.get("value", "unknown agent error")
            logger.warning("Agent reported error: %s", err_msg)
            return {"_error": True, "_error_message": err_msg}

        # Phase B：模型输出的宏动作单元（与单动作互斥；子动作由
        # _execute_action_unit 规范化为白名单动作并校验 ref）
        if isinstance(tc_dict.get("unit"), list) and tc_dict.get("unit"):
            return {"unit": tc_dict.get("unit"), "thinking": tc_dict.get("thinking")}

        # 普通操作：将 PlaywrightMCPToolCall 映射为 send_act 期望的格式
        return {
            "name": action_name,
            "tool": action_name,
            "args": {
                "selector": tc_dict.get("selector"),
                "element_desc": tc_dict.get("element_desc"),
                "value": tc_dict.get("value"),
                "timeout_ms": tc_dict.get("timeout_ms", 30000),
                "snapshot_version": current_snapshot_version,
            },
        }

    # ── 持久化 ──────────────────────────────────────────────────────────────

    async def _execute_script_action(self, action: dict) -> dict:
        """036+：执行链 run_script（客户端链同口径）——服务端沙箱执行，结果回喂模型。"""
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

        if getattr(self, "_script_user_id", None) is None:
            self._script_user_id = await resolve_script_user_id(
                self.db, batch_id=getattr(self, "_batch_id", None),
            )
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

    async def _execute_captcha_action(
        self, agent_id: str, run_id_str: str, action: dict,
    ) -> dict:
        """037：执行链验证码识别与填写（客户端链）。

        - 开关 exec_captcha_enabled（默认开）；关闭 = 拒绝并给出配置提示
        - 识别器：make_model_captcha_solver（模型读图，无本地 OCR；执行 Agent 模型通道）
        - 浏览器适配：ClientBrowser（复用既有 evaluate/fill/click 基础动作 → 旧客户端无需升级）
        """
        try:
            from app.config import get_settings

            if not bool(getattr(get_settings(), "exec_captcha_enabled", True)):
                return {"success": False, "error": "验证码动作已关闭（exec_captcha_enabled=false）"}
        except Exception:
            pass
        try:
            from core.captcha import make_model_captcha_solver, solve_captcha_via_browser
            from core.chat.browse import ClientBrowser

            browser = ClientBrowser(self.agent_manager, str(agent_id), run_id_str)

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

    async def _execute_fetch_action(
        self, agent_id: str, run_id_str: str, action: dict,
    ) -> dict:
        """036：fetch 动作的客户端链编排（bridge 驱动既有 evaluate 序列）。

        客户端只需支持既有 evaluate 动作（结果文本回传，见 client_core 文本白名单）；
        证据字节经分块回传后在服务端落盘（客户端旧版本亦可工作）。
        """
        from core.fetch_action import run_fetch_action

        async def _eval(tool: str, args: dict) -> dict:
            res = await asyncio.wait_for(
                self.agent_manager.send_act(
                    agent_id, run_id_str,
                    {"action": "evaluate", "value": args.get("function") or ""},
                ),
                timeout=90,
            )
            return res or {}

        # 动作字典有两种形态：_llm_decide 返回 {name, tool, args:{value…}}；
        # 单动作路径为顶层平铺 {action, value…}（服务端 runner 即平铺）。
        # 036 实测：只读顶层导致 fetch 永远报「需要 value JSON」（run 448）。
        _nested = action.get("args") if isinstance(action.get("args"), dict) else {}
        _value = action.get("value")
        if _value in (None, ""):
            _value = _nested.get("value")
        try:
            out = await run_fetch_action(_eval, _value or "")
        except Exception as exc:  # noqa: BLE001 — 可读失败即可
            logger.warning("fetch 编排失败: %s", exc)
            return {"success": False, "error": str(exc)}

        payload: dict = {"api": out.get("api"), "token": out.get("token")}
        ev = out.get("evidence")
        if ev:
            if self._evidence_dir:
                from core.fetch_action import save_fetch_evidence_file

                payload.update(
                    save_fetch_evidence_file(
                        self._evidence_dir,
                        ev,
                        out.get("filename"),
                        (out.get("api") or {}).get("content_type") or "",
                    )
                )
            else:
                payload["evidence_error"] = "无证据目录（跳过落盘）"
        if out.get("evidence_error"):
            payload["evidence_error"] = out["evidence_error"]
        if not out.get("success"):
            return {
                "success": False,
                "error": out.get("error") or "fetch 执行失败",
                "fetch": payload,
            }
        return {"success": True, "error": None, "fetch": payload}

    async def _record_turn(
        self,
        run_id: int,
        turn: int,
        snapshot: str,
        action: dict[str, Any],
        result: dict[str, Any],
    ) -> None:
        """将一轮 OTA 记录到 agent_messages 和 agent_tool_calls 表。"""
        # 用户消息（快照上下文，截断避免超长）
        snapshot_preview = snapshot[:2000] + ("..." if len(snapshot) > 2000 else "")

        await crud_agent_run.create_message(
            self.db, run_id, turn, "user",
            f"[Snapshot] {snapshot_preview}",
        )

        # 助手消息（LLM 决策）
        await crud_agent_run.create_message(
            self.db, run_id, turn, "assistant",
            str(action),
        )

        # 工具调用记录（032：uncertain 附加键随 args 入库，供报告标注）
        _tool_args = dict(action.get("args", {}) or {})
        if result.get("_uncertain"):
            _tool_args["_uncertain"] = True
        # 036：fetch 的 api 元数据 / 证据路径并入 tool_args（报告标注「以 API 方式完成」）
        _fetch = result.get("fetch") if isinstance(result, dict) else None
        if isinstance(_fetch, dict):
            _api = _fetch.get("api") or {}
            if _api:
                _tool_args["api"] = {k: v for k, v in _api.items() if k != "excerpt"}
                if _api.get("excerpt"):
                    _tool_args["api"]["excerpt"] = str(_api["excerpt"])[:500]
            if _fetch.get("evidence_path"):
                _tool_args["evidence_path"] = _fetch["evidence_path"]
                _tool_args["evidence_size"] = _fetch.get("evidence_size")
                _tool_args["evidence_truncated"] = bool(_fetch.get("evidence_truncated"))
            if _fetch.get("evidence_error"):
                _tool_args["evidence_error"] = str(_fetch["evidence_error"])[:200]
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
            _tool_args["script"] = _meta
            if _script.get("title"):
                _tool_args["title"] = str(_script["title"])[:100]
        await crud_agent_run.create_tool_call(
            self.db, run_id, turn,
            str(action.get("name") or action.get("action") or "unknown"),
            _tool_args,
            result.get("success", False),
            result.get("error", ""),
        )


async def create_pending_agent_run(
    db, agent_def, case_id: int, agent_name: str, environment_id: int | None = None,
    user_id: int | None = None, batch_id: int | None = None,
) -> object:
    """创建 pending AgentRun 记录，由跨 worker poller 在正确 worker 上执行 OTA"""
    from app.db_models import AgentRun as AgentRunModel
    from app.tz import now as tz_now

    goal = {"type": "client_exec", "case_id": case_id, "agent_name": agent_name}
    if environment_id:
        goal["environment_id"] = environment_id
    if user_id:
        goal["user_id"] = user_id
    if batch_id:
        goal["batch_id"] = batch_id

    ar = AgentRunModel(
        agent_definition_id=agent_def.id,
        case_id=case_id,
        goal=goal,
        status="pending",
    )
    db.add(ar)
    await db.commit()
    await db.refresh(ar)
    return ar
