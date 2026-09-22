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
from datetime import datetime, timezone
from typing import Any

from app.crud import agent_run as crud_agent_run
from app.db_models import AgentRun
from app.runtime_config import resolve_prompt_for_agent
from app.tz import now as tz_now
from core.llm_wrapper import create_openai_client, generate_tool_call
from core.ota_cursor import (
    OTA_CURSOR_SYSTEM_PROMPT,
    bridge_click_xy_act,
    bridge_evaluate_act,
    find_candidate,
    parse_bbox_center_from_text,
    should_capture_screenshot,
)

logger = logging.getLogger(__name__)


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
    from core.goal_agent_loop import journal_entry_covers_checklist

    for i, s in enumerate(steps or []):
        order = int(s.get("step_order") or s.get("step_number") or i + 1)
        if order in covered_orders:
            continue
        desc = str(s.get("description") or s.get("original_description") or "")
        if journal_entry_covers_checklist(entry, step_description=desc):
            return order
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
        self._environment_id = environment_id
        self._reuse_browser_session = bool((goal or {}).get("reuse_browser_session"))
        self._screenshot_dir: str | None = None
        self._screenshot_paths: dict[str, str] = {}
        self._active_agent_id: str | None = None
        self._active_run_id_str: str | None = None
        self._batch_id: int | None = existing_batch_id

        # 固化脚本优先：case 有匹配 hash 的 compiled_script 时先回放（秒级、无 LLM），
        # 失败/不支持再回退 OTA 循环（与 nl_goal 链路同语义：回放失败不阻断执行）。
        try:
            if await self._try_replay_compiled_script(run, agent_id, run_id_str):
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

        # 发送 RUN_START 让 Agent 启动 MCP / 浏览器
        try:
            await self.agent_manager.send(agent_id, {
                "type": "run_start", "run_id": run_id_str,
                "description": "AI Agent execution",
                "num_steps": 0,
                "backend": "playwright_mcp",
                "reuse_existing_browser": bool(
                    getattr(self, "_reuse_browser_session", False)
                ),
                "navigate_base_url": False,
            })
            await asyncio.sleep(1)  # 等 MCP 初始化
        except Exception as e:
            logger.warning("Bridge RUN_START failed for run %s: %s", run_id_str, e)

        try:
            import os as _os
            _d = _os.path.join("reports", f"agent_{run.id}_{tz_now().strftime('%Y%m%d_%H%M%S')}", "screenshots")
            _os.makedirs(_d, exist_ok=True)
            self._screenshot_dir = _d
        except Exception:
            pass

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

    async def _capture_failure_screenshot(
        self, agent_id: str, run_id_str: str, name: str = "failure_final",
    ) -> str | None:
        """失败终止前强制 observe+截图，保证报告有现场图。"""
        try:
            obs = await asyncio.wait_for(
                self.agent_manager.send_observe(
                    agent_id, run_id_str, want_screenshot=True,
                ),
                timeout=30,
            )
            if isinstance(obs, dict):
                return self._save_screenshot(obs.get("screenshot_b64"), name)
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
                         name=(tc.name if tc and tc.name else ""))
        self.db.add(batch)
        await self.db.commit()
        await self.db.refresh(batch)
        self._batch_id = batch.id
        logger.info("Bridge: batch #%d created (running)", batch.id)

    async def _save_report(self, run: AgentRun, case_id: int) -> None:
        """创建 TestRun / RunBatch 报告记录，含步骤日志。"""
        from core.runner._persistence import save_run_results
        from app.tz import now as tz_now
        from app.crud import get_test_case
        from app.db_models import RunBatch

        # 创建或更新 RunBatch
        tc = await get_test_case(self.db, case_id)
        run_status_map = "passed" if run.status in ("completed", "passed") else "failed"
        if self._batch_id:
            from sqlalchemy import select as _sl
            from app.db_models import RunBatch as _RB
            r = await self.db.execute(_sl(_RB).where(_RB.id == self._batch_id))
            batch = r.scalar_one_or_none()
            if batch:
                batch.status = run_status_map
                batch.passed = 1 if run_status_map == "passed" else 0
                batch.failed = 1 if run_status_map == "failed" else 0
        if not self._batch_id or not batch:
            batch = RunBatch(
                status=run_status_map,
                triggered_by=f"agent:{self.agent_def.name}",
                project_id=tc.project_id if tc else 0,
                total_cases=1,
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

        # 从 agent_tool_calls 构建步骤日志
        logs: list[dict] = []
        try:
            from sqlalchemy import select
            from app.db_models import AgentToolCall
            r = await self.db.execute(
                select(AgentToolCall).where(
                    AgentToolCall.run_id == run.id
                ).order_by(AgentToolCall.turn_number)
            )
            for tc in r.scalars().all():
                level = "info" if tc.success == 1 else "error"
                tool = tc.tool_name or ""
                args = tc.tool_args or {}
                _LABELS = {"browser_navigate":"导航到","browser_click":"点击","browser_type":"输入",
                           "browser_snapshot":"查看页面","browser_take_screenshot":"截图",
                           "browser_wait_for":"等待","browser_select_option":"选择"}
                lbl = _LABELS.get(tool, tool)
                vals = [v for v in [args.get("url"), args.get("value"), args.get("selector"), args.get("element"), args.get("text")] if v]
                msg = f"{lbl} {vals[0]}" if vals else lbl
                if tc.error_message:
                    msg += f" | {tc.error_message}"
                # 027: 仅失败步骤保留截图证据，通过步骤不留图
                ss_path = None
                if tc.success != 1:
                    ss_path = self._screenshot_paths.get(f"turn_{tc.turn_number:02d}_observe") \
                        or self._screenshot_paths.get(f"turn_{tc.turn_number:02d}_act") \
                        or self._last_screenshot_path()
                logs.append({
                    "step_id": None,
                    "level": level,
                    "message": msg,
                    "screenshot_path": ss_path,
                })
        except Exception as _tce:
            logger.warning("Could not load tool calls for run %d: %s", run.id, _tce)

        if self._replay_logs:
            logs = list(self._replay_logs)

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
        await save_run_results(
            case_id=case_id,
            status=run_status_map,
            start_time=run.started_at or tz_now(),
            end_time=run.completed_at or tz_now(),
            duration=((run.completed_at or tz_now()) - (run.started_at or tz_now())).total_seconds(),
            report_path=None,
            log_path=None,
            logs=logs,
            batch_id=batch.id,
        )
        logger.info("Bridge: report saved for run #%d (batch #%d, %d steps)", run.id, batch.id, len(logs))

    async def _try_replay_compiled_script(
        self, run: AgentRun, agent_id: str, run_id_str: str
    ) -> bool:
        """回放固化脚本；成功即完成 run（返回 True），失败/不支持返回 False 交给 OTA。"""
        from app.crud import get_test_case
        from core.compiled_script import steps_content_hash

        tc = await get_test_case(self.db, run.case_id)
        script = (getattr(tc, "compiled_script", None) or "").strip() if tc else ""
        if not script:
            return False
        steps = await self._load_case_steps(run.case_id)
        if not steps:
            return False
        current_hash = steps_content_hash(steps)
        stored_hash = getattr(tc, "compiled_script_hash", None) or ""
        if stored_hash and stored_hash != current_hash:
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
        args = action.get("args") or {}
        entry: dict = {
            "turn": turn,
            "success": True,
            "status": "ok",
            "action": name,
            "selector": args.get("selector"),
            "value": args.get("value"),
            "element_desc": args.get("element_desc"),
        }
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
            if failed_turns >= max_failed_turns:
                await self._fail(
                    run.id,
                    f"失败轮达到上限 ({failed_turns}/{max_failed_turns})；"
                    f"总轮次 {turn}",
                )
                return
            turn += 1
            logger.info(
                "━━━ Turn %d/%d (总 %d/%d) (run #%d) ━━━",
                failed_turns + 1, max_failed_turns, turn, max_total_turns, run.id,
            )

            # ── 1. Observe: WS 取快照（失败轮/困难页按需截图）──
            if carried_obs is not None:
                obs = carried_obs
                carried_obs = None
                logger.debug("Bridge reusing post-act snapshot (turn %d)", turn)
            else:
                want_shot = failed_turns > 0
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

            # ── 2. Think: LLM 决策 ──
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
                    failed_turns += 1
                    logger.info(
                        "Bridge refusing premature done: uncovered steps %s "
                        "(covered %s/%s, run #%d turn %d)",
                        _missed, len(self._journal_covered), len(case_steps),
                        run.id, turn,
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

            # ── 3. Act: WS 执行 ──
            try:
                result = await asyncio.wait_for(
                    self.agent_manager.send_act(agent_id, run_id_str, action),
                    timeout=120,
                )
            except asyncio.TimeoutError:
                logger.warning("Act timeout at turn %d", turn)
                result = {"success": False, "error": "Act timeout (120s)"}
            except Exception as exc:
                logger.exception("Act error at turn %d", turn)
                result = {"success": False, "error": str(exc)}

            # ── 3b. Act 失败恢复：Cursor click_xy 兜底 + stale ref 刷新 ──
            if not result.get("success"):
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
                            result = await asyncio.wait_for(
                                self.agent_manager.send_act(agent_id, run_id_str, action),
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
            await self._record_turn(run.id, turn, snapshot, action, result)

            # ── 5. 更新上下文 ──
            action_name = action.get("name", action.get("action", "?"))
            context_messages.append({
                "role": "assistant",
                "content": f"{action_name}: {action.get('args', {})}",
            })
            from core.mcp_args import build_tool_status
            status_text = build_tool_status(result)
            context_messages.append({"role": "tool", "content": status_text})

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

        step_description = (
            f"GOAL: {goal}\n\n"
            f"TEST CASE STEPS (complete ALL of them, one at a time):\n{steps_text}\n\n"
            f"HISTORY (recent actions and results):\n{context_text}\n"
            f"CURRENT URL: {page_url}\n"
            f"{nav_instruction}\n\n"
            f"IMPORTANT: You MUST complete ALL test steps above before returning done. "
            f"Only return done after verifying the last step's expected result. "
            f"Based on the PAGE CONTENT below, decide the SINGLE NEXT ACTION "
            f"to move towards the GOAL. "
            f"If stuck or impossible, use action='error' with explanation in value."
            f"\n\nSNAPSHOT VERSION: {current_snapshot_version}"
            f"\nCANDIDATE ELEMENTS (choose selector only from this list):\n"
            f"{serialize_candidates(candidates) or '(none)'}"
        )
        step_description += (
            f"\n\nHARD RULES: "
            f"(a) NEVER repeat an action whose tool result was successful. "
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
        except Exception:
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

        # 工具调用记录
        await crud_agent_run.create_tool_call(
            self.db, run_id, turn,
            action.get("name", action.get("action", "unknown")),
            action.get("args", {}),
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
