"""客户端 Agent 执行端点 — 浏览器跑在远端 Agent 上。

- POST /api/testcases/{case_id}/run-client    — 单用例推到 Agent
- POST /api/testcases/batch-run-client       — 批量推到 Agent

每个端点都会:
1. 选可用 Agent（按名称匹配或取第一个）
2. 创建 RunBatch
3. 起后台任务调 agent_manager.execute_on_agent
4. 把结果写报告 + DB
5. 失败保持浏览器打开，成功则发 SHUTDOWN
"""
from __future__ import annotations

import asyncio as _asyncio
import json as _json
import logging
import os as _os
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app import crud, db_models
from app.crud import agent_definition as crud_agent_definition
from app.auth import get_current_user, get_user_project_filter
from app import database as db_mod
from app.database import get_async_db
from app.tz import now as tz_now
from core.runner import save_run_results

from ._schemas import BatchCaseIdsRequest

logger = logging.getLogger(__name__)


def _write_json(path: str, data: dict) -> None:
    """同步写入 JSON 文件 — 供 asyncio.to_thread 调用。"""
    with open(path, "w", encoding="utf-8") as f:
        _json.dump(data, f, ensure_ascii=False, indent=2)


def _ensure_dir(path: str) -> None:
    """同步创建目录 — 供 asyncio.to_thread 调用。"""
    _os.makedirs(path, exist_ok=True)


async def _resolve_execution_base_url(
    project_id: int,
    environment_id: Optional[int] = None,
) -> Optional[str]:
    """解析执行用 BASE URL：优先环境配置，其次项目 base_url。"""
    async with db_mod.AsyncSessionLocal() as db:
        if environment_id:
            from app.crud.environment import get_environment
            env = await get_environment(db, environment_id)
            if env and (env.base_url or "").strip():
                return env.base_url.strip()
        project = await crud.get_project(db, project_id)
        if project and (getattr(project, "base_url", None) or "").strip():
            return project.base_url.strip()
    return None


router = APIRouter()


async def _find_online_agent_in_db(db: AsyncSession, agent_name: str | None) -> dict | None:
    """跨 worker 兜底：查 DB 中最近 120s 内有心跳或 status=online 的 Agent。"""
    try:
        result = await db.execute(
            text("SELECT id, name FROM agents WHERE (last_heartbeat > NOW() - INTERVAL '120 seconds' OR (status='online' AND last_heartbeat IS NULL))"
                 + (" AND name=:name" if agent_name else "")),
            {"name": agent_name} if agent_name else {},
        )
        row = result.first()
        if row:
            return {"id": row[0], "name": row[1]}
    except Exception:
        pass
    return None


async def _ensure_agent_def_id(db: AsyncSession) -> int:
    """返回一个有效的 agent_definition_id，用于 client 执行的 AgentRun 记录。"""
    try:
        r = await db.execute(select(db_models.AgentDefinition).limit(1))
        first = r.scalar_one_or_none()
        if first:
            return first.id
    except Exception:
        pass
    return 1


async def _create_pending_execution(
    db: AsyncSession, case_id: int, agent_name: str | None, db_case, batch_id: int | None = None,
    environment_id: Optional[int] = None,
    is_init: bool = False,
    seq: int | None = None,
) -> dict:
    """在 DB 中创建待执行记录，由拥有 Agent WS 连接的 worker 轮询接管。"""
    from app.db_models import AgentRun
    from app.models.schemas import AgentRunCreate
    agent_name_text = agent_name or "unknown"

    # 取一个有效的 agent_definition_id 作为队列标识
    dummy_def_id = 1
    try:
        from app.db_models import AgentDefinition as _AD
        r = await db.execute(select(_AD).limit(1))
        first = r.scalar_one_or_none()
        if first:
            dummy_def_id = first.id
    except Exception:
        pass

    goal = {"type": "client_exec", "case_id": case_id, "agent_name": agent_name_text}
    if batch_id:
        goal["batch_id"] = batch_id
    if environment_id:
        goal["environment_id"] = environment_id
    if is_init:
        goal["is_init"] = True
    if seq is not None:
        goal["seq"] = int(seq)

    ar = AgentRun(
        agent_definition_id=dummy_def_id,
        goal=goal,
        status="pending",
    )
    db.add(ar)
    await db.commit()
    await db.refresh(ar)

    return {
        "id": ar.id,
        "case_id": case_id,
        "status": "queued",
        "agent_name": agent_name_text,
        "message": f"用例已排队，等待 {agent_name_text} 执行",
    }


@router.post("/{case_id}/run-client")
async def run_test_case_on_client(
    case_id: int,
    user=Depends(get_current_user),
    agent_name: Optional[str] = None,
    environment_id: Optional[int] = None,
    backend: Optional[str] = None,
    db: AsyncSession = Depends(get_async_db),
) -> dict:
    """Run a test case on a connected client agent via WebSocket.

    Query ``backend``: ``playwright_mcp`` | ``browser_use`` | ``hybrid``（默认 hybrid，定位失败同浏览器救场）。
    """
    from agent.manager import agent_manager
    from app.runtime_config import execution_backend_config

    db_case = await crud.get_test_case(db, case_id)
    if db_case is None:
        raise HTTPException(status_code=404, detail="Test case not found")

    if backend is None:
        backend = execution_backend_config.backend or "hybrid"
        if getattr(db_case, "case_kind", None) == "ui" and backend == "playwright_mcp":
            backend = "hybrid"

    if backend is not None and backend not in ("playwright_mcp", "browser_use", "hybrid"):
        raise HTTPException(
            status_code=400,
            detail="backend must be playwright_mcp, browser_use, or hybrid",
        )

    allowed_ids = get_user_project_filter(user)
    if allowed_ids is not None and db_case.project_id not in allowed_ids:
        raise HTTPException(status_code=404, detail="Test case not found")

    # 检查是否有活跃 execution AgentDefinition — 跨 worker 创建 pending run，由 poller 接管
    from core.agent_ota import should_use_ota_agent
    active_agent_def = await crud_agent_definition.get_active_by_type(db, "execution")
    if should_use_ota_agent(active_agent_def) and agent_name and (await _find_online_agent_in_db(db, agent_name)):
        # 在 DB 中创建 AgentRun（status=pending），poller 会在有 WS 连接的 worker 上调起 OTA
        from core.agent_bridge import create_pending_agent_run
        arun = await create_pending_agent_run(db, active_agent_def, db_case.id, agent_name, environment_id)
        return {"message": f"Agent #{arun.id} queued via AI Agent", "agent_run_id": arun.id}

    agents = await agent_manager.get_online_agents()
    if not agents:
        db_agent = await _find_online_agent_in_db(db, agent_name)
        if not db_agent:
            raise HTTPException(status_code=400, detail="No client agents available")
        return await _create_pending_execution(db, case_id, agent_name, db_case, environment_id=environment_id)
    if agent_name:
        matched = [a for a in agents if a.name == agent_name]
        if not matched:
            raise HTTPException(status_code=400, detail=f"Agent '{agent_name}' not found or offline")
        agent = matched[0]
    else:
        agent = agents[0]

    if backend in ("browser_use", "hybrid") and "browser_use" not in (agent.capabilities or []):
        raise HTTPException(
            status_code=400,
            detail=(
                f"Agent '{agent.name}' 未声明 browser_use 能力。"
                "请在客户端安装 browser-use 并重启 Agent。"
            ),
        )

    # 同 worker 路径：显式 OTA skill 时走 AgentBridge
    active_agent_def_same = active_agent_def or (await crud_agent_definition.get_active_by_type(db, "execution"))
    if should_use_ota_agent(active_agent_def_same) and agent_name:
        from core.agent_bridge import AgentBridge
        bridge = AgentBridge(agent_manager, db, active_agent_def_same)
        arun = await bridge.orchestrate(
            case_id=db_case.id,
            agent_id=agent_name,
            goal={"type": "client_exec", "case_id": db_case.id, "agent_name": agent_name},
            environment_id=environment_id,
        )
        return {"message": f"Agent #{arun.id} executing via AI Agent", "agent_run_id": arun.id}

    run_id = uuid.uuid4().hex[:12]

    steps_raw = await crud.get_steps_for_case(db, case_id)
    steps = [
        {
            "id": s.id,
            "step_order": s.step_order,
            "description": s.description,
            "expected_result": s.parsed_result,
            "learned_locator": getattr(s, "learned_locator", None)
            if isinstance(getattr(s, "learned_locator", None), dict)
            else None,
            "structured_step": getattr(s, "structured_step", None)
            if isinstance(getattr(s, "structured_step", None), dict)
            else None,
            "cacheable": bool(getattr(s, "cacheable", True)),
        }
        for s in sorted(steps_raw, key=lambda x: x.step_order)
    ]

    if not steps:
        raise HTTPException(status_code=400, detail="Test case has no steps")

    # 解析 BASE URL：环境优先，否则回退项目 base_url
    base_url_override = await _resolve_execution_base_url(db_case.project_id, environment_id)
    if base_url_override:
        logger.info("Client run BASE URL: %s (env_id=%s)", base_url_override, environment_id)
    else:
        logger.warning(
            "Client run has no BASE URL (case=%s env_id=%s) — browser may stay on about:blank",
            case_id, environment_id,
        )

    batch = await crud.create_run_batch(db, project_id=db_case.project_id, total_cases=1, triggered_by=getattr(user, 'username', None))

    async def _run() -> None:
        start_time = tz_now()
        output_dir = _os.path.join("reports", f"run_{case_id}_{start_time.strftime('%Y%m%d_%H%M%S')}")
        await _asyncio.to_thread(_ensure_dir, output_dir)

        _all_success = True
        agent_run_id = None
        db_run_id = None

        # 预创建 TestRun，避免长跑（browser-use）期间轮询批次因「无 runs」误判失败
        try:
            async with db_mod.AsyncSessionLocal() as _pr_db:
                pending = db_models.TestRun(
                    case_id=case_id,
                    batch_id=batch.id,
                    status="running",
                    start_time=start_time,
                    end_time=start_time,
                )
                _pr_db.add(pending)
                await _pr_db.commit()
                await _pr_db.refresh(pending)
                db_run_id = pending.id
        except Exception:
            logger.exception("Failed to precreate TestRun for client exec")

        # 创建 AgentRun 记录
        try:
            async with db_mod.AsyncSessionLocal() as _ar_db:
                def_id = await _ensure_agent_def_id(_ar_db)
                ar = db_models.AgentRun(
                    agent_definition_id=def_id,
                    case_id=case_id,
                    goal={"type": "client_exec", "case_id": case_id, "agent_name": agent.name},
                    status="running",
                    started_at=start_time,
                )
                _ar_db.add(ar)
                await _ar_db.commit()
                await _ar_db.refresh(ar)
                agent_run_id = ar.id
        except Exception:
            logger.exception("Failed to create AgentRun record")

        try:
            step_results = await agent_manager.execute_on_agent(
                agent.id, run_id, db_case.name, steps, output_dir=output_dir,
                base_url_override=base_url_override,
                backend=backend,
            )
            try:
                from core.locator_memory import persist_learned_locators_from_results
                # Use a fresh session — request-scoped db may be stale after long agent runs
                async with db_mod.AsyncSessionLocal() as _persist_db:
                    orm_steps = await crud.get_steps_for_case(_persist_db, case_id)
                    by_order = {s.step_order: s for s in orm_steps}
                    await persist_learned_locators_from_results(
                        _persist_db, step_results, steps_by_order=by_order,
                    )
            except Exception:
                logger.warning("persist learned_locator after client exec failed", exc_info=True)
            # empty list: all([]) is True in Python — treat as failed
            all_passed = bool(step_results) and all(r.get("success") for r in step_results)
            status = "passed" if all_passed else "failed"
            if not all_passed:
                _all_success = False

            report = {
                "test_case_id": case_id,
                "test_case_name": db_case.name,
                "status": status,
                "start_time": start_time.isoformat(),
                "end_time": tz_now().isoformat(),
                "duration": (tz_now() - start_time).total_seconds(),
                "steps": step_results,
            }
            report_path = _os.path.join(output_dir, "report.json")
            await _asyncio.to_thread(_write_json, report_path, report)

            await save_run_results(
                case_id, status, start_time, tz_now(),
                (tz_now() - start_time).total_seconds(),
                report_path, None, [], batch_id=batch.id, run_id=db_run_id,
            )

            # 更新 AgentRun 状态
            if agent_run_id:
                try:
                    async with db_mod.AsyncSessionLocal() as _ar_db:
                        await crud.update_agent_run_status(_ar_db, agent_run_id, status)
                except Exception:
                    pass
        except Exception:
            logger.exception("Client execution failed")
            _all_success = False
            end_time = tz_now()

            # 更新 AgentRun 状态为失败
            if agent_run_id:
                try:
                    async with db_mod.AsyncSessionLocal() as _ar_db:
                        await crud.update_agent_run_status(_ar_db, agent_run_id, "failed")
                except Exception:
                    pass

            await save_run_results(
                case_id, "failed", start_time, end_time,
                (end_time - start_time).total_seconds(),
                None, None,
                [{"level": "error", "message": "客户端 Agent 执行过程中发生内部错误，请查看服务端日志获取详情"}],
                batch_id=batch.id, run_id=db_run_id,
            )

        async with db_mod.AsyncSessionLocal() as _db:
            _result = await _db.execute(
                select(db_models.RunBatch).where(db_models.RunBatch.id == batch.id)
            )
            _batch = _result.scalar_one_or_none()
            if _batch:
                await crud._compute_batch_status(_db, _batch)
                await _db.commit()

        if _all_success:
            try:
                from agent.models import WSMessage, WSMessageType
                session = await agent_manager.get_session(agent.id)
                if session:
                    await session.send(WSMessage(
                        type=WSMessageType.SHUTDOWN, agent_id=agent.id,
                    ))
                    logger.info("All cases passed — shutdown signal sent to agent")
            except Exception as exc:
                logger.warning("Failed to send shutdown to agent: %s", exc, exc_info=True)
        else:
            logger.info("Some cases failed — browser left open for debugging")

    _task = _asyncio.create_task(_run())
    async def _on_run_done(t: _asyncio.Task) -> None:
        exc = t.exception()
        if exc:
            logger.error("Client agent run task failed: %s", exc)
            try:
                
                from app import db_models as _dm
                async with db_mod.AsyncSessionLocal() as _db:
                    _result = await _db.execute(
                        select(_dm.RunBatch).where(_dm.RunBatch.id == batch.id)
                    )
                    _b = _result.scalar_one_or_none()
                    if _b and _b.status in ("running", "pending"):
                        _b.status = "failed"
                        _b.finished_at = tz_now()
                        await _db.commit()
            except Exception:
                logger.warning("Failed to mark batch %s as failed", batch.id, exc_info=True)
    _task.add_done_callback(lambda t: _asyncio.ensure_future(_on_run_done(t)))

    return {
        "message": f"Test case {case_id} running on client agent {agent.name}",
        "run_id": run_id,
        "batch_id": batch.id,
    }


@router.post("/batch-run-client")
async def batch_run_client(body: BatchCaseIdsRequest, user=Depends(get_current_user), db: AsyncSession = Depends(get_async_db)) -> dict:
    """Run multiple test cases sequentially on a connected client agent."""
    from agent.manager import agent_manager

    agents = await agent_manager.get_online_agents()
    if not agents:
        db_agent = await _find_online_agent_in_db(db, body.agent_name)
        if not db_agent:
            raise HTTPException(status_code=400, detail="No client agents available")
        # 跨 worker：创建统一 RunBatch；初始化用例先入队，再入队主用例（按 id 顺序执行）
        from app.db_models import RunBatch
        queued = []
        tc0 = None
        init_ids = list(body.init_case_ids or [])
        main_ids = list(body.case_ids or [])
        ordered_ids: list[int] = []
        seen: set[int] = set()
        for cid in init_ids + main_ids:
            if cid in seen:
                continue
            seen.add(cid)
            ordered_ids.append(cid)
        init_set = set(init_ids)
        for cid in ordered_ids:
            tc = await crud.get_test_case(db, cid)
            if tc and tc0 is None:
                tc0 = tc
        batch = RunBatch(
            status="running",
            project_id=tc0.project_id if tc0 else 0,
            total_cases=len(ordered_ids),
            triggered_by=body.agent_name,
        )
        db.add(batch)
        await db.commit()
        await db.refresh(batch)
        for seq, cid in enumerate(ordered_ids):
            tc = await crud.get_test_case(db, cid)
            if tc:
                pend = await _create_pending_execution(
                    db, cid, body.agent_name, tc,
                    batch_id=batch.id, environment_id=body.environment_id,
                    is_init=cid in init_set, seq=seq,
                )
                queued.append(cid)
        if queued:
            return {
                "status": "queued",
                "case_ids": queued,
                "agent_name": body.agent_name,
                "message": f"{len(queued)} cases queued for batch #{batch.id} (init first)",
            }
        raise HTTPException(status_code=400, detail="No test cases to queue")
    
    if body.agent_name:
        matched = [a for a in agents if a.name == body.agent_name]
        if not matched:
            raise HTTPException(status_code=400, detail=f"Agent '{body.agent_name}' not found or offline")
        agent = matched[0]
    else:
        agent = agents[0]

    if body.backend is not None and body.backend not in ("playwright_mcp", "browser_use", "hybrid"):
        raise HTTPException(status_code=400, detail="backend must be playwright_mcp, browser_use, or hybrid")
    if body.backend in ("browser_use", "hybrid") and "browser_use" not in (agent.capabilities or []):
        raise HTTPException(
            status_code=400,
            detail=(
                f"Agent '{agent.name}' 未声明 browser_use 能力。"
                "请在客户端安装 browser-use 并重启 Agent。"
            ),
        )

    case_ids = body.case_ids
    init_case_ids = body.init_case_ids or []
    if not case_ids:
        raise HTTPException(status_code=400, detail="No test case IDs provided")

    # 严格顺序：初始化用例在前（去重），再跑主用例（排除已作为 init 的 id）
    ordered_ids: list[int] = []
    seen: set[int] = set()
    for cid in list(init_case_ids) + list(case_ids):
        if cid in seen:
            continue
        seen.add(cid)
        ordered_ids.append(cid)
    init_set = set(init_case_ids)

    async def _load_case_info(cid: int) -> Optional[dict]:
        tc = await crud.get_test_case(db, cid)
        if not tc:
            return None
        steps_raw = await crud.get_steps_for_case(db, cid)
        steps = [
            {
                "id": s.id,
                "step_order": s.step_order,
                "description": s.description,
                "expected_result": s.parsed_result,
                "learned_locator": getattr(s, "learned_locator", None)
                if isinstance(getattr(s, "learned_locator", None), dict)
                else None,
                "structured_step": getattr(s, "structured_step", None)
                if isinstance(getattr(s, "structured_step", None), dict)
                else None,
                "cacheable": bool(getattr(s, "cacheable", True)),
            }
            for s in sorted(steps_raw, key=lambda x: x.step_order)
        ]
        return {
            "id": tc.id,
            "name": tc.name,
            "project_id": tc.project_id,
            "steps": steps,
            "is_init": cid in init_set,
        }

    case_infos = [await _load_case_info(cid) for cid in ordered_ids]
    case_infos = [c for c in case_infos if c]
    if not case_infos:
        raise HTTPException(status_code=400, detail="No valid test cases found")

    logger.info(
        "Client batch order (%s cases): %s",
        len(case_infos),
        [(c["id"], c["name"], "init" if c["is_init"] else "main") for c in case_infos],
    )

    allowed_ids = get_user_project_filter(user)
    project_id = case_infos[0]["project_id"]
    if allowed_ids is not None and project_id not in allowed_ids:
        raise HTTPException(status_code=404, detail="No valid test cases found")

    # 解析 BASE URL：环境优先，否则回退项目 base_url（批量共用）
    base_url_override = await _resolve_execution_base_url(project_id, body.environment_id)
    if base_url_override:
        logger.info(
            "Client batch BASE URL: %s (env_id=%s)",
            base_url_override, body.environment_id,
        )
    else:
        logger.warning(
            "Client batch has no BASE URL (project=%s env_id=%s)",
            project_id, body.environment_id,
        )

    batch = await crud.create_run_batch(db, project_id=project_id, total_cases=len(case_infos), triggered_by=getattr(user, 'username', None))

    async def _run_batch() -> None:
        _all_success = True
        for idx, info in enumerate(case_infos):
            case_id = info["id"]
            steps = info["steps"]
            if not steps:
                logger.warning("Skip case %s — no steps", case_id)
                continue

            # 仅第一个用例导航 BASE URL；后续复用同一浏览器会话（保留登录态）
            navigate_base = idx == 0

            run_id = uuid.uuid4().hex[:12]
            start_time = tz_now()
            output_dir = _os.path.join("reports", f"run_{case_id}_{start_time.strftime('%Y%m%d_%H%M%S')}")
            await _asyncio.to_thread(_ensure_dir, output_dir)

            logger.info(
                "Client batch case %s/%s: id=%s name=%r init=%s navigate=%s",
                idx + 1, len(case_infos), case_id, info["name"], info.get("is_init"), navigate_base,
            )

            # 检查是否有活跃 execution AgentDefinition — 显式 OTA 时走桥接
            try:
                async with db_mod.AsyncSessionLocal() as _ad_db:
                    from core.agent_ota import should_use_ota_agent
                    active_agent_def = await crud_agent_definition.get_active_by_type(_ad_db, "execution")
                    if should_use_ota_agent(active_agent_def) and body.agent_name:
                        from core.agent_bridge import AgentBridge
                        bridge = AgentBridge(agent_manager, _ad_db, active_agent_def)
                        agent_manager._agent_busy.add(agent.id)
                        try:
                            await bridge.orchestrate(
                                case_id=case_id,
                                agent_id=body.agent_name,
                                goal={"type": "client_exec", "case_id": case_id, "agent_name": body.agent_name},
                                environment_id=body.environment_id,
                                existing_batch_id=batch.id,
                            )
                        finally:
                            agent_manager._agent_busy.discard(agent.id)
                        continue
            except Exception:
                logger.exception("AgentDefinition check failed for batch case %s", case_id)

            # 创建 AgentRun 记录
            agent_run_id = None
            try:
                async with db_mod.AsyncSessionLocal() as _ar_db:
                    def_id = await _ensure_agent_def_id(_ar_db)
                    ar = db_models.AgentRun(
                        agent_definition_id=def_id,
                        case_id=case_id,
                        goal={"type": "client_exec", "case_id": case_id, "agent_name": agent.name},
                        status="running",
                        started_at=start_time,
                    )
                    _ar_db.add(ar)
                    await _ar_db.commit()
                    await _ar_db.refresh(ar)
                    agent_run_id = ar.id
            except Exception:
                logger.exception("Failed to create AgentRun record for batch case %s", case_id)

            case_failed = False
            try:
                step_results = await agent_manager.execute_on_agent(
                    agent.id, run_id, info["name"], steps, output_dir=output_dir,
                    base_url_override=base_url_override,
                    backend=getattr(body, "backend", None),
                    navigate_base_url=navigate_base,
                )
                try:
                    from core.locator_memory import persist_learned_locators_from_results
                    # Batch path: steps_raw is local to _load_case_info — reload ORM rows here
                    async with db_mod.AsyncSessionLocal() as _persist_db:
                        orm_steps = await crud.get_steps_for_case(_persist_db, case_id)
                        by_order = {s.step_order: s for s in orm_steps}
                        await persist_learned_locators_from_results(
                            _persist_db, step_results, steps_by_order=by_order,
                        )
                except Exception:
                    logger.warning(
                        "persist learned_locator after batch client exec failed",
                        exc_info=True,
                    )
                # empty list: all([]) is True — must not mark batch as success
                all_passed = bool(step_results) and all(r.get("success") for r in step_results)
                status = "passed" if all_passed else "failed"
                if not all_passed:
                    _all_success = False
                    case_failed = True

                report = {
                    "test_case_id": case_id,
                    "test_case_name": info["name"],
                    "status": status,
                    "start_time": start_time.isoformat(),
                    "end_time": tz_now().isoformat(),
                    "duration": (tz_now() - start_time).total_seconds(),
                    "steps": step_results,
                }
                report_path = _os.path.join(output_dir, "report.json")
                await _asyncio.to_thread(_write_json, report_path, report)

                await save_run_results(
                    case_id, status, start_time, tz_now(),
                    (tz_now() - start_time).total_seconds(),
                    report_path, None, [], batch_id=batch.id,
                    is_init=info.get("is_init", False),
                )

                # 更新 AgentRun 状态
                if agent_run_id:
                    try:
                        async with db_mod.AsyncSessionLocal() as _ar_db:
                            await crud.update_agent_run_status(_ar_db, agent_run_id, status)
                    except Exception:
                        pass
            except Exception:
                logger.exception("Agent run failed for case %s", case_id)
                _all_success = False
                case_failed = True
                end_time = tz_now()

                # 更新 AgentRun 状态为失败
                if agent_run_id:
                    try:
                        async with db_mod.AsyncSessionLocal() as _ar_db:
                            await crud.update_agent_run_status(_ar_db, agent_run_id, "failed")
                    except Exception:
                        pass

                await save_run_results(
                    case_id, "failed", start_time, end_time,
                    (end_time - start_time).total_seconds(),
                    None, None,
                    [{"level": "error", "message": "客户端 Agent 执行过程中发生内部错误，请查看服务端日志获取详情"}],
                    batch_id=batch.id,
                    is_init=info.get("is_init", False),
                )

            # 初始化用例失败则中止后续，避免在未登录会话上乱序继续
            if case_failed and info.get("is_init"):
                logger.warning(
                    "Init case %s failed — abort remaining %s case(s) in batch",
                    case_id, len(case_infos) - idx - 1,
                )
                for remaining in case_infos[idx + 1:]:
                    await save_run_results(
                        remaining["id"], "failed", tz_now(), tz_now(), 0.0,
                        None, None,
                        [{
                            "level": "error",
                            "message": f"因初始化用例 {case_id} 失败而跳过",
                        }],
                        batch_id=batch.id,
                        is_init=remaining.get("is_init", False),
                    )
                break

        async with db_mod.AsyncSessionLocal() as _db:
            _result = await _db.execute(
                select(db_models.RunBatch).where(db_models.RunBatch.id == batch.id)
            )
            _b = _result.scalar_one_or_none()
            if _b:
                await crud._compute_batch_status(_db, _b)
                await _db.commit()

        if _all_success:
            try:
                from agent.models import WSMessage, WSMessageType
                session = await agent_manager.get_session(agent.id)
                if session:
                    await session.send(WSMessage(
                        type=WSMessageType.SHUTDOWN, agent_id=agent.id,
                    ))
                    logger.info("All cases passed — shutdown signal sent to agent")
                else:
                    logger.info("All cases passed — browser left open for debugging")
            except Exception as exc:
                logger.warning("Failed to send shutdown to agent: %s", exc, exc_info=True)

    _task = _asyncio.create_task(_run_batch())
    async def _on_batch_done(t: _asyncio.Task) -> None:
        exc = t.exception()
        if exc:
            logger.error("Client agent batch-run task failed: %s", exc)
            try:
                
                from app import db_models as _dm
                async with db_mod.AsyncSessionLocal() as _db:
                    _result = await _db.execute(
                        select(_dm.RunBatch).where(_dm.RunBatch.id == batch.id)
                    )
                    _b = _result.scalar_one_or_none()
                    if _b and _b.status in ("running", "pending"):
                        _b.status = "failed"
                        _b.finished_at = tz_now()
                        await _db.commit()
            except Exception:
                logger.warning("Failed to mark batch %s as failed", batch.id, exc_info=True)
    _task.add_done_callback(lambda t: _asyncio.ensure_future(_on_batch_done(t)))

    return {
        "message": f"{len(case_ids)} case(s) running on client agent {agent.name}",
        "batch_id": batch.id,
    }
