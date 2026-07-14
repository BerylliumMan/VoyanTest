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
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import crud, db_models
from app.auth import get_current_user
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


router = APIRouter()


@router.post("/{case_id}/run-client")
async def run_test_case_on_client(case_id: int, user=Depends(get_current_user), agent_name: Optional[str] = None, environment_id: Optional[int] = None, db: AsyncSession = Depends(get_async_db)) -> dict:
    """Run a test case on a connected client agent via WebSocket."""
    from agent.manager import agent_manager

    db_case = await crud.get_test_case(db, case_id)
    if db_case is None:
        raise HTTPException(status_code=404, detail="Test case not found")

    agents = await agent_manager.get_online_agents()
    if not agents:
        raise HTTPException(status_code=400, detail="No client agents available")

    if agent_name:
        matched = [a for a in agents if a.name == agent_name]
        if not matched:
            raise HTTPException(status_code=400, detail=f"Agent '{agent_name}' not found or offline")
        agent = matched[0]
    else:
        agent = agents[0]
    run_id = uuid.uuid4().hex[:12]

    steps_raw = await crud.get_steps_for_case(db, case_id)
    steps = [
        {"step_order": s.step_order, "description": s.description, "expected_result": s.parsed_result}
        for s in sorted(steps_raw, key=lambda x: x.step_order)
    ]

    if not steps:
        raise HTTPException(status_code=400, detail="Test case has no steps")

    # 解析环境配置中的 base_url
    base_url_override = None
    if environment_id:
        from app.database import AsyncSessionLocal
        async with AsyncSessionLocal() as _env_db:
            from app.crud.environment import get_environment
            env = await get_environment(_env_db, environment_id)
            if env:
                base_url_override = env.base_url

    batch = await crud.create_run_batch(db, project_id=db_case.project_id, total_cases=1, triggered_by=getattr(user, 'username', None))

    async def _run() -> None:
        start_time = tz_now()
        output_dir = _os.path.join("reports", f"run_{case_id}_{start_time.strftime('%Y%m%d_%H%M%S')}")
        await _asyncio.to_thread(_ensure_dir, output_dir)

        _all_success = True
        try:
            step_results = await agent_manager.execute_on_agent(
                agent.id, run_id, db_case.name, steps, output_dir=output_dir,
                base_url_override=base_url_override,
            )
            all_passed = all(r["success"] for r in step_results)
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
                report_path, None, [], batch_id=batch.id,
            )
        except Exception:
            logger.exception("Client execution failed")
            _all_success = False
            end_time = tz_now()
            await save_run_results(
                case_id, "failed", start_time, end_time,
                (end_time - start_time).total_seconds(),
                None, None,
                [{"level": "error", "message": "客户端 Agent 执行过程中发生内部错误，请查看服务端日志获取详情"}],
                batch_id=batch.id,
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
        raise HTTPException(status_code=400, detail="No client agents available")

    if body.agent_name:
        matched = [a for a in agents if a.name == body.agent_name]
        if not matched:
            raise HTTPException(status_code=400, detail=f"Agent '{body.agent_name}' not found or offline")
        agent = matched[0]
    else:
        agent = agents[0]
    case_ids = body.case_ids
    init_case_ids = body.init_case_ids or []
    if not case_ids:
        raise HTTPException(status_code=400, detail="No test case IDs provided")

    async def _load_case_info(cid: int) -> Optional[dict]:
        tc = await crud.get_test_case(db, cid)
        if not tc:
            return None
        steps_raw = await crud.get_steps_for_case(db, cid)
        steps = [
            {"step_order": s.step_order, "description": s.description, "expected_result": s.parsed_result}
            for s in sorted(steps_raw, key=lambda x: x.step_order)
        ]
        return {"id": tc.id, "name": tc.name, "project_id": tc.project_id, "steps": steps, "is_init": cid in init_case_ids}

    # 解析环境配置中的 base_url（批量路径，所有用例共用）
    base_url_override = None
    if body.environment_id:
        from app.database import AsyncSessionLocal
        async with AsyncSessionLocal() as _env_db:
            from app.crud.environment import get_environment
            env = await get_environment(_env_db, body.environment_id)
            if env:
                base_url_override = env.base_url

    all_case_ids = init_case_ids + case_ids
    case_infos = [await _load_case_info(cid) for cid in all_case_ids]
    case_infos = [c for c in case_infos if c]
    if not case_infos:
        raise HTTPException(status_code=400, detail="No valid test cases found")

    batch = await crud.create_run_batch(db, project_id=case_infos[0]["project_id"], total_cases=len(case_infos), triggered_by=getattr(user, 'username', None))

    async def _run_batch() -> None:
        _all_success = True
        for info in case_infos:
            case_id = info["id"]
            steps = info["steps"]
            if not steps:
                continue

            run_id = uuid.uuid4().hex[:12]
            start_time = tz_now()
            output_dir = _os.path.join("reports", f"run_{case_id}_{start_time.strftime('%Y%m%d_%H%M%S')}")
            await _asyncio.to_thread(_ensure_dir, output_dir)

            try:
                step_results = await agent_manager.execute_on_agent(
                    agent.id, run_id, info["name"], steps, output_dir=output_dir,
                    base_url_override=base_url_override,
                )
                all_passed = all(r["success"] for r in step_results)
                status = "passed" if all_passed else "failed"
                if not all_passed:
                    _all_success = False

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
            except Exception:
                logger.exception("Agent run failed for case %s", case_id)
                _all_success = False
                end_time = tz_now()
                await save_run_results(
                    case_id, "failed", start_time, end_time,
                    (end_time - start_time).total_seconds(),
                    None, None,
                    [{"level": "error", "message": "客户端 Agent 执行过程中发生内部错误，请查看服务端日志获取详情"}],
                    batch_id=batch.id,
                    is_init=info.get("is_init", False),
                )

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
