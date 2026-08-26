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

logger = logging.getLogger(__name__)


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
    ) -> AgentRun:
        """OTA 主循环入口。

        Args:
            case_id: 测试用例 ID
            agent_id: Agent 名称（对应 agent_manager 中注册的 id）
            goal: 目标快照（含 type, case_id, agent_name 等字段）
            existing_run_id: 复用已有 AgentRun（跨 worker poller 路径）
            environment_id: 环境 ID，用于获取 base_url
            existing_batch_id: 复用已有 RunBatch（批量执行路径）

        Returns:
            已 commit/refresh 的 AgentRun 实例，status 为 completed 或 failed
        """
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

        # 发送 RUN_START 让 Agent 启动 MCP / 浏览器
        try:
            await self.agent_manager.send(agent_id, {
                "type": "run_start", "run_id": run_id_str,
                "description": "AI Agent execution",
                "num_steps": 0,
            })
            await asyncio.sleep(1)  # 等 MCP 初始化
        except Exception as e:
            logger.warning("Bridge RUN_START failed for run %s: %s", run_id_str, e)

        self._environment_id = environment_id
        self._screenshot_dir: str | None = None
        self._screenshot_paths: dict[str, str] = {}
        self._batch_id: int | None = existing_batch_id
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

        return run

    def _save_screenshot(self, b64_data: str | None, name: str) -> str | None:
        """将 base64 截图保存到文件，返回文件路径。"""
        if not b64_data or not self._screenshot_dir:
            return None
        try:
            import base64, os
            path = os.path.join(self._screenshot_dir, f"{name}.png")
            if "," in b64_data:
                b64_data = b64_data.split(",", 1)[1]
            with open(path, "wb") as f:
                f.write(base64.b64decode(b64_data))
            self._screenshot_paths[name] = path
            return path
        except Exception:
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
                         project_id=tc.project_id if tc else 0, total_cases=1)
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
            )
            self.db.add(batch)
        await self.db.commit()
        await self.db.refresh(batch)
        self._batch_id = batch.id

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
                        or self._screenshot_paths.get(f"turn_{tc.turn_number:02d}_act")
                logs.append({
                    "step_id": None,
                    "level": level,
                    "message": msg,
                    "screenshot_path": ss_path,
                })
        except Exception as _tce:
            logger.warning("Could not load tool calls for run %d: %s", run.id, _tce)

        # 无工具调用但失败时，写一条错误日志
        if not logs and run.status in ("failed", "error"):
            err_msg = run.error or "执行失败，无详细步骤记录"
            logs.append({"step_id": None, "level": "error", "message": err_msg, "screenshot_path": None})

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
                for tr_id in tr_ids:
                    await self.db.execute(
                        _text("INSERT INTO run_logs (run_id, level, message, timestamp) "
                              "VALUES (:rid, 'error', :msg, now())"),
                        {"rid": tr_id, "msg": f"执行失败: {error[:180]}"},
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
        successful_acts = 0
        assertion_passed = False

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
                else:
                    reason = nav_result.get("error") or "unknown"
                    msg = f"导航失败，已停止执行: {case_url} — {reason}"
                    logger.error("Bridge: %s", msg)
                    await self._fail(run.id, msg)
                    return
            except Exception as e:
                msg = f"导航失败，已停止执行: {case_url} — {e}"
                logger.error("Bridge: %s", msg)
                await self._fail(run.id, msg)
                return

        for turn in range(1, max_turns + 1):
            logger.info("━━━ Turn %d/%d (run #%d) ━━━", turn, max_turns, run.id)

            # ── 1. Observe: WS 取快照 ──
            try:
                obs = await asyncio.wait_for(
                    self.agent_manager.send_observe(agent_id, run_id_str),
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
                if consecutive_failures >= 3:
                    await self._fail(run.id, f"Observe failed {consecutive_failures} times: {err}")
                    return
                continue

            consecutive_failures = 0
            snapshot = obs.get("snapshot", "")
            page_url = obs.get("page_url", "")

            # 保存观察截图
            self._save_screenshot(obs.get("screenshot_b64"), f"turn_{turn:02d}_observe")

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
            )

            if action is None:
                # LLM 调用失败 — 跳过本轮继续
                logger.warning("LLM decision returned None at turn %d, continuing", turn)
                continue

            # 检查停止信号
            if action.get("_done"):
                await crud_agent_run.create_message(
                    self.db, run.id, turn, "assistant",
                    f"目标已达成: {action.get('_summary', '')}",
                )
                await self._complete(run.id, turn)
                return

            if action.get("_error"):
                # Agent reported error — 双重熔断（027-e2e-fixes 迭代）：
                #   同因(指纹相同)第 2 次 / 连续任意 error 第 3 次 → 终止
                _err_msg = str(action.get("_error_message", ""))
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

            # ── 3b. Act 失败恢复（025-ref-click US3：stale/hallucinated ref 刷新重试）──
            if not result.get("success") and retries_used < 1:
                error = result.get("error", "") or ""
                from core.self_healing import build_failure_hint, is_stale_ref_error

                if is_stale_ref_error(error):
                    retries_used += 1
                    try:
                        obs_retry = await asyncio.wait_for(
                            self.agent_manager.send_observe(agent_id, run_id_str),
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
            successful_acts = len(successful_act_keys)
            # 成功操作打断 error 连击（027 熔断重置点）
            if result.get("success"):
                _consec_err_n = 0
                _last_error_fp = None
            if "断言通过" in status_text:
                assertion_passed = True

            # ── 6. 引擎侧完成判定（025-ref-click：不依赖模型自觉）──
            # 条件一：最后动作是断言且成功，且成功操作数 ≥ 步骤数
            # 条件二：本轮已出现过断言成功，且成功操作数 ≥ 步骤数
            #        （防模型无视证据重复执行步骤）
            engine_done = case_steps and successful_acts >= len(case_steps) and (
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

        # 达到最大轮次
        await self._fail(run.id, f"Max turns ({max_turns}) reached")

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
    ) -> dict[str, Any] | None:
        """LLM 决策：根据当前页面状态和上下文决定下一步操作。

        Returns:
            dict with "name"/"args" keys for normal actions,
            dict with "_done": True for goal-completion signal,
            dict with "_error": True for agent error signal,
            None if LLM call failed (skip turn).
        """
        # 解析 Agent 特定系统提示词
        system_prompt = await resolve_prompt_for_agent(
            self.db, "execution", "step_execute",
            variables={"goal": goal, "snapshot": snapshot},
        )

        # 构建上下文历史文本（最近 5 轮 = 10 条消息）
        context_text = ""
        for msg in context_messages[-10:]:
            context_text += f"[{msg['role']}] {msg['content']}\n"

        # 构建测试步骤文本
        steps_text = ""
        if case_steps:
            steps_text = "\n".join(
                f"  Step {i+1}: {s}" for i, s in enumerate(case_steps)
            )
        nav_instruction = ""
        if case_url and (not page_url or "about:blank" in page_url):
            nav_instruction = (
                f"\n第一步操作：使用 browser_navigate 打开 {case_url}"
            )

        step_description = (
            f"GOAL: {goal}\n\n"
            f"TEST CASE STEPS (complete ALL of them, one at a time):\n{steps_text}\n\n"
            f"HISTORY (recent actions and results):\n{context_text}\n"
            f"CURRENT URL: {page_url}\n"
            f"{nav_instruction}\n\n"
            f"IMPORTANT: You MUST complete ALL test steps above before returning done. "
            f"Only return done after verifying the last step's expected result. "
            f"After each action, call browser_snapshot or browser_take_screenshot "
            f"to verify the result matches the expected outcome. "
            f"If a step fails, report the error and continue to the next step. "
            f"Based on the PAGE CONTENT below, decide the SINGLE NEXT ACTION "
            f"to move towards the GOAL. "
            f"If stuck or impossible, use action='error' with explanation in value."
        )
        # 025-ref-click: 防止模型不信任工具结果而重复执行/无限截图
        step_description += (
            f"\n\nHARD RULES: "
            f"(a) NEVER repeat an action whose tool result was successful. "
            f"(b) Screenshots do NOT verify anything — only wait/assert_text results count. "
            f"(c) When HISTORY shows every test-case step has a successful tool result, "
            f"you MUST immediately return done. No exceptions."
        )
        if extra_hint:
            step_description += f"\n\nRETRY CONTEXT: {extra_hint}"

        try:
            tool_call = await generate_tool_call(
                step_description=step_description,
                dom_snapshot=snapshot,
                client=llm_client,
                system_prompt=system_prompt or None,
                agent_type="execution",
                db=self.db,
            )
        except Exception:
            logger.exception("LLM decision failed at turn %d", turn)
            return None

        tc_dict = tool_call.model_dump()
        action_name = tc_dict.get("action", "")

        # 停止信号：done / error
        if action_name == "done":
            logger.info("Agent declared goal achieved: %s", tc_dict.get("value", ""))
            return {"_done": True, "summary": tc_dict.get("value", "")}

        if action_name == "error":
            err_msg = tc_dict.get("value", "unknown agent error")
            logger.warning("Agent reported error: %s", err_msg)
            return {"_error": True, "_error_message": err_msg}

        # 普通操作：将 PlaywrightMCPToolCall 映射为 send_act 期望的格式
        # （025-ref-click: element_desc 供 MCP 权限上下文，timeout_ms 全链路透传）
        return {
            "name": action_name,
            "tool": action_name,
            "args": {
                "selector": tc_dict.get("selector"),
                "element_desc": tc_dict.get("element_desc"),
                "value": tc_dict.get("value"),
                "timeout_ms": tc_dict.get("timeout_ms", 30000),
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
) -> object:
    """创建 pending AgentRun 记录，由跨 worker poller 在正确 worker 上执行 OTA"""
    from app.db_models import AgentRun as AgentRunModel
    from app.tz import now as tz_now

    goal = {"type": "client_exec", "case_id": case_id, "agent_name": agent_name}
    if environment_id:
        goal["environment_id"] = environment_id

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
