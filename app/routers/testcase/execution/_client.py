"""客户端 Agent 执行端点 — 浏览器跑在远端 Agent 上（仅 OTA / AgentBridge）。

- POST /api/testcases/{case_id}/run-client    — 单用例推到 Agent
- POST /api/testcases/batch-run-client       — 批量推到 Agent

每个端点都会:
1. 校验活跃 execution AgentDefinition（须启用工具）
2. 选可用 Agent（本 worker WS 或 DB 心跳跨 worker）
3. 走 AgentBridge / create_pending_agent_run（poller 接管）
"""
from __future__ import annotations

import asyncio as _asyncio
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app import crud, db_models
from app.crud import agent_definition as crud_agent_definition
from app.auth import get_current_user, get_user_project_filter
from app import database as db_mod
from app.database import get_async_db

from ._schemas import BatchCaseIdsRequest

logger = logging.getLogger(__name__)

_OTA_AGENT_REQUIRED = (
    "OTA 执行需要启用工具的活跃 execution AgentDefinition，"
    "请先在设置中配置并启用至少一个工具"
)

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


async def _create_pending_execution(
    db: AsyncSession,
    case_id: int,
    agent_name: str | None,
    agent_def,
    batch_id: int | None = None,
    user_id: int | None = None,
    environment_id: Optional[int] = None,
    is_init: bool = False,
    seq: int | None = None,
    reuse_browser_session: bool = False,
) -> dict:
    """在 DB 中创建待执行 OTA 记录，由拥有 Agent WS 连接的 worker 轮询接管。"""
    from app.db_models import AgentRun

    agent_name_text = agent_name or "unknown"
    def_id = getattr(agent_def, "id", None) or 1

    goal = {"type": "client_exec", "case_id": case_id, "agent_name": agent_name_text}
    if batch_id:
        goal["batch_id"] = batch_id
    if user_id:
        goal["user_id"] = user_id
    if environment_id:
        goal["environment_id"] = environment_id
    if is_init:
        goal["is_init"] = True
    if seq is not None:
        goal["seq"] = int(seq)
    if reuse_browser_session:
        goal["reuse_browser_session"] = True

    ar = AgentRun(
        agent_definition_id=def_id,
        case_id=case_id,
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


async def _require_ota_agent_def(db: AsyncSession):
    """返回启用工具的活跃 execution AgentDefinition，否则 HTTP 400。"""
    from core.agent_ota import should_use_ota_agent

    active = await crud_agent_definition.get_active_by_type(db, "execution")
    if not should_use_ota_agent(active):
        raise HTTPException(status_code=400, detail=_OTA_AGENT_REQUIRED)
    return active


@router.post("/{case_id}/run-client")
async def run_test_case_on_client(
    case_id: int,
    user=Depends(get_current_user),
    agent_name: Optional[str] = None,
    environment_id: Optional[int] = None,
    backend: Optional[str] = None,
    db: AsyncSession = Depends(get_async_db),
) -> dict:
    """Run a test case on a connected client agent via OTA AgentBridge.

    Query ``backend``: 仅 ``ota``（历史名称会规范化为 ota）。
    """
    from agent.manager import agent_manager
    from app.runtime_config import normalize_execution_backend
    from core.agent_bridge import AgentBridge, create_pending_agent_run

    db_case = await crud.get_test_case(db, case_id)
    if db_case is None:
        raise HTTPException(status_code=404, detail="Test case not found")

    backend = normalize_execution_backend(backend)

    allowed_ids = get_user_project_filter(user)
    if allowed_ids is not None and db_case.project_id not in allowed_ids:
        raise HTTPException(status_code=404, detail="Test case not found")

    active_agent_def = await _require_ota_agent_def(db)

    agents = await agent_manager.get_online_agents()
    if not agent_name:
        if agents:
            agent_name = agents[0].name
        else:
            db_agent = await _find_online_agent_in_db(db, None)
            if not db_agent:
                raise HTTPException(status_code=400, detail="No client agents available")
            agent_name = db_agent["name"]

    # 跨 worker：Agent 在 DB 在线 → pending，由持有 WS 的 worker poller 接管
    if await _find_online_agent_in_db(db, agent_name):
        arun = await create_pending_agent_run(
            db, active_agent_def, db_case.id, agent_name, environment_id,
            user_id=getattr(user, "id", None),
        )
        return {"message": f"Agent #{arun.id} queued via AI Agent", "agent_run_id": arun.id}

    # 本 worker：DB 心跳可能滞后，但本地有 WS
    if not agents:
        raise HTTPException(status_code=400, detail="No client agents available")
    matched = [a for a in agents if a.name == agent_name]
    if not matched:
        raise HTTPException(status_code=400, detail=f"Agent '{agent_name}' not found or offline")
    agent = matched[0]

    _sw_batch = await crud.create_run_batch(
        db, project_id=db_case.project_id, name=db_case.name or "",
        total_cases=1, triggered_by=getattr(user, "username", None),
    )
    bridge = AgentBridge(agent_manager, db, active_agent_def)
    arun = await bridge.orchestrate(
        case_id=db_case.id,
        agent_id=agent.name,
        goal={"type": "client_exec", "case_id": db_case.id, "agent_name": agent.name},
        environment_id=environment_id,
        existing_batch_id=_sw_batch.id,
        notify_user_id=getattr(user, "id", None),
    )
    return {
        "message": f"Agent #{arun.id} executing via AI Agent",
        "agent_run_id": arun.id,
        "backend": backend,
    }


@router.post("/batch-run-client")
async def batch_run_client(body: BatchCaseIdsRequest, user=Depends(get_current_user), db: AsyncSession = Depends(get_async_db)) -> dict:
    """Run multiple test cases sequentially on a connected client agent via OTA."""
    from agent.manager import agent_manager
    from app.runtime_config import normalize_execution_backend
    from core.agent_bridge import AgentBridge
    from core.runner._persistence import build_batch_execution_queue, precreate_pending_runs

    body.backend = normalize_execution_backend(body.backend)

    active_agent_def = await _require_ota_agent_def(db)

    case_ids = body.case_ids
    init_case_ids = body.init_case_ids or []
    init_policy = getattr(body, "init_policy", None) or "before_each"
    if not case_ids:
        raise HTTPException(status_code=400, detail="No test case IDs provided")

    agents = await agent_manager.get_online_agents()
    agent_name = body.agent_name

    if agent_name:
        if agents:
            matched = [a for a in agents if a.name == agent_name]
            if matched:
                agent = matched[0]
            else:
                if not await _find_online_agent_in_db(db, agent_name):
                    raise HTTPException(
                        status_code=400,
                        detail=f"Agent '{agent_name}' not found or offline",
                    )
                agent = None  # 跨 worker
                agents = []
        else:
            if not await _find_online_agent_in_db(db, agent_name):
                raise HTTPException(status_code=400, detail="No client agents available")
            agent = None
    else:
        if agents:
            agent = agents[0]
            agent_name = agent.name
            body.agent_name = agent_name
        else:
            db_agent = await _find_online_agent_in_db(db, None)
            if not db_agent:
                raise HTTPException(status_code=400, detail="No client agents available")
            agent_name = db_agent["name"]
            body.agent_name = agent_name
            agent = None

    exec_queue = build_batch_execution_queue(list(case_ids), list(init_case_ids), init_policy)

    # 加载用例基本信息（权限 / 批次名）；步骤由 Bridge 自行加载
    case_infos: list[dict] = []
    for cid, is_init in exec_queue:
        tc = await crud.get_test_case(db, cid)
        if tc:
            case_infos.append({
                "id": tc.id,
                "name": tc.name,
                "project_id": tc.project_id,
                "is_init": is_init,
            })
    if not case_infos:
        raise HTTPException(status_code=400, detail="No valid test cases found")

    allowed_ids = get_user_project_filter(user)
    project_id = case_infos[0]["project_id"]
    if allowed_ids is not None and project_id not in allowed_ids:
        raise HTTPException(status_code=404, detail="No valid test cases found")

    # 跨 worker：无本机 WS → pending 队列
    if not agents or agent is None:
        from app.db_models import RunBatch

        reuse_session = len(exec_queue) > 1
        batch = RunBatch(
            status="running",
            project_id=project_id,
            total_cases=len(exec_queue),
            triggered_by=agent_name,
        )
        db.add(batch)
        await db.commit()
        await db.refresh(batch)

        queued = []
        for seq, (cid, is_init) in enumerate(exec_queue):
            tc = await crud.get_test_case(db, cid)
            if tc:
                await _create_pending_execution(
                    db, cid, agent_name, active_agent_def,
                    batch_id=batch.id, user_id=getattr(user, "id", None),
                    environment_id=body.environment_id,
                    is_init=is_init, seq=seq,
                    reuse_browser_session=reuse_session,
                )
                queued.append(cid)
        if queued:
            return {
                "status": "queued",
                "case_ids": queued,
                "agent_name": agent_name,
                "backend": body.backend,
                "message": f"{len(queued)} cases queued for batch #{batch.id} (policy={init_policy})",
            }
        raise HTTPException(status_code=400, detail="No test cases to queue")

    logger.info(
        "Client batch order (%s slots, policy=%s): %s",
        len(case_infos),
        init_policy,
        [(c["id"], c["name"], "init" if c["is_init"] else "main") for c in case_infos],
    )

    _batch_name = case_infos[0].get("name") or ""
    if len(case_infos) > 1:
        _batch_name = f"{_batch_name} 等{len(case_infos)}个用例"
    batch = await crud.create_run_batch(
        db, project_id=project_id, name=_batch_name,
        total_cases=len(case_infos), triggered_by=getattr(user, "username", None),
    )

    async with db_mod.AsyncSessionLocal() as _pr_db:
        await precreate_pending_runs(
            _pr_db, list(case_ids), batch.id,
            init_case_ids=list(init_case_ids), init_policy=init_policy,
        )

    async def _run_batch() -> None:
        from app import execution_control
        from app.services.notifications import notify_batch_completed
        from app.tz import now as tz_now
        from core.runner import save_run_results

        _stopped_by_user = False
        try:
            for idx, info in enumerate(case_infos):
                await execution_control.wait_if_paused(batch.id)
                if execution_control.is_stopped(batch.id):
                    _stopped_by_user = True
                    logger.info("Batch %s stopped — abort remaining cases", batch.id)
                    async with db_mod.AsyncSessionLocal() as _stop_db:
                        await crud.cancel_remaining_batch_runs(
                            _stop_db, batch.id, message="用户停止执行",
                        )
                        await _stop_db.commit()
                    break

                case_id = info["id"]
                reuse_session = len(case_infos) > 1

                try:
                    from core.agent_ota import should_use_ota_agent

                    async with db_mod.AsyncSessionLocal() as _ad_db:
                        agent_def = await crud_agent_definition.get_active_by_type(
                            _ad_db, "execution",
                        )
                        if not should_use_ota_agent(agent_def):
                            await save_run_results(
                                case_id, "failed", tz_now(), tz_now(), 0.0,
                                None, None,
                                [{"level": "error", "message": _OTA_AGENT_REQUIRED}],
                                batch_id=batch.id,
                                is_init=info.get("is_init", False),
                            )
                            if info.get("is_init"):
                                break
                            continue
                        bridge = AgentBridge(agent_manager, _ad_db, agent_def)
                        agent_manager._agent_busy.add(agent.id)
                        try:
                            await bridge.orchestrate(
                                case_id=case_id,
                                agent_id=agent_name,
                                goal={
                                    "type": "client_exec",
                                    "case_id": case_id,
                                    "agent_name": agent_name,
                                    "batch_id": batch.id,
                                    "seq": idx,
                                    "is_init": info.get("is_init", False),
                                    "reuse_browser_session": reuse_session,
                                },
                                environment_id=body.environment_id,
                                existing_batch_id=batch.id,
                                notify_user_id=getattr(user, "id", None),
                            )
                        finally:
                            agent_manager._agent_busy.discard(agent.id)
                except Exception:
                    logger.exception("AgentBridge failed for batch case %s", case_id)
                    if info.get("is_init"):
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

        finally:
            from app import execution_control as _ec
            await _ec.clear_batch(batch.id)

        async with db_mod.AsyncSessionLocal() as _db:
            _result = await _db.execute(
                select(db_models.RunBatch).where(db_models.RunBatch.id == batch.id)
            )
            _b = _result.scalar_one_or_none()
            if _b:
                if _b.status not in ("cancelled", "paused") and _b.finished_at is None:
                    _b.finished_at = tz_now()
                await crud._compute_batch_status(_db, _b)
                await _db.commit()
                _uid2 = getattr(user, "id", None)
                if _uid2 and not _stopped_by_user:
                    _asyncio.create_task(notify_batch_completed(_b.id, _uid2))

    from app import execution_control as _ec_reg
    _task = _asyncio.create_task(_run_batch())
    await _ec_reg.register_batch_task(batch.id, _task)

    async def _on_batch_done(t: _asyncio.Task) -> None:
        from app.tz import now as tz_now
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
        "backend": body.backend,
    }
