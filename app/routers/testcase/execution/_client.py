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
from sqlalchemy.orm import Session

from app import crud, db_models
from app.database import SessionLocal, get_db
from app.tz import now as tz_now
from core.runner import save_run_results

from ._schemas import BatchCaseIdsRequest

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/{case_id}/run-client")
async def run_test_case_on_client(case_id: int, agent_name: Optional[str] = None, db: Session = Depends(get_db)) -> dict:
    """Run a test case on a connected client agent via WebSocket."""
    from agent.manager import agent_manager

    db_case = crud.get_test_case(db, case_id)
    if db_case is None:
        raise HTTPException(status_code=404, detail="Test case not found")

    agents = agent_manager.get_online_agents()
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

    steps_raw = crud.get_steps_for_case(db, case_id)
    steps = [
        {"step_order": s.step_order, "description": s.description, "expected_result": s.parsed_result}
        for s in sorted(steps_raw, key=lambda x: x.step_order)
    ]

    if not steps:
        raise HTTPException(status_code=400, detail="Test case has no steps")

    batch = crud.create_run_batch(db, project_id=db_case.project_id, total_cases=1)

    async def _run() -> None:
        start_time = tz_now()
        output_dir = _os.path.join("reports", f"run_{case_id}_{start_time.strftime('%Y%m%d_%H%M%S')}")
        _os.makedirs(output_dir, exist_ok=True)

        _all_success = True
        try:
            step_results = await agent_manager.execute_on_agent(
                agent.id, run_id, db_case.name, steps, output_dir=output_dir,
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
            with open(report_path, "w", encoding="utf-8") as f:
                _json.dump(report, f, ensure_ascii=False, indent=2)

            save_run_results(
                case_id, status, start_time, tz_now(),
                (tz_now() - start_time).total_seconds(),
                report_path, None, [], batch_id=batch.id,
            )
        except Exception as e:
            logger.error(f"Client execution failed: {e}")
            _all_success = False
            end_time = tz_now()
            save_run_results(
                case_id, "failed", start_time, end_time,
                (end_time - start_time).total_seconds(),
                None, None,
                [{"level": "error", "message": "客户端 Agent 执行过程中发生内部错误，请查看服务端日志获取详情"}],
                batch_id=batch.id,
            )

        _db = SessionLocal()
        try:
            _batch = _db.query(db_models.RunBatch).filter(db_models.RunBatch.id == batch.id).first()
            if _batch:
                crud._compute_batch_status(_db, _batch)
                _db.commit()
        finally:
            _db.close()

        if _all_success:
            try:
                from agent.models import WSMessage, WSMessageType
                session = agent_manager.get_session(agent.id)
                if session:
                    await session.send(WSMessage(
                        type=WSMessageType.SHUTDOWN, agent_id=agent.id,
                    ))
                    logger.info("All cases passed — shutdown signal sent to agent")
            except Exception as exc:
                logger.warning(f"Failed to send shutdown to agent: {exc}")
        else:
            logger.info("Some cases failed — browser left open for debugging")

    _task = _asyncio.create_task(_run())
    def _on_run_done(t) -> None:
        exc = t.exception()
        if exc:
            logger.error(f"Client agent run task failed: {exc}")
            try:
                from app.database import SessionLocal as _SL
                from app import db_models as _dm
                _db = _SL()
                _b = _db.query(_dm.RunBatch).filter(_dm.RunBatch.id == batch.id).first()
                if _b and _b.status in ("running", "pending"):
                    _b.status = "failed"
                    _b.finished_at = tz_now()
                    _db.commit()
                _db.close()
            except Exception as e:
                logger.warning(f"Failed to mark batch {batch.id} as failed: {e}")
    _task.add_done_callback(_on_run_done)

    return {
        "message": f"Test case {case_id} running on client agent {agent.name}",
        "run_id": run_id,
        "batch_id": batch.id,
    }


@router.post("/batch-run-client")
async def batch_run_client(body: BatchCaseIdsRequest, db: Session = Depends(get_db)) -> dict:
    """Run multiple test cases sequentially on a connected client agent."""
    from agent.manager import agent_manager

    agents = agent_manager.get_online_agents()
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

    def _load_case_info(cid: int) -> Optional[dict]:
        tc = crud.get_test_case(db, cid)
        if not tc:
            return None
        steps_raw = crud.get_steps_for_case(db, cid)
        steps = [
            {"step_order": s.step_order, "description": s.description, "expected_result": s.parsed_result}
            for s in sorted(steps_raw, key=lambda x: x.step_order)
        ]
        return {"id": tc.id, "name": tc.name, "project_id": tc.project_id, "steps": steps, "is_init": cid in init_case_ids}

    all_case_ids = init_case_ids + case_ids
    case_infos = [_load_case_info(cid) for cid in all_case_ids]
    case_infos = [c for c in case_infos if c]
    if not case_infos:
        raise HTTPException(status_code=400, detail="No valid test cases found")

    batch = crud.create_run_batch(db, project_id=case_infos[0]["project_id"], total_cases=len(case_infos))

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
            _os.makedirs(output_dir, exist_ok=True)

            try:
                step_results = await agent_manager.execute_on_agent(
                    agent.id, run_id, info["name"], steps, output_dir=output_dir,
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
                with open(report_path, "w", encoding="utf-8") as f:
                    _json.dump(report, f, ensure_ascii=False, indent=2)

                save_run_results(
                    case_id, status, start_time, tz_now(),
                    (tz_now() - start_time).total_seconds(),
                    report_path, None, [], batch_id=batch.id,
                    is_init=info.get("is_init", False),
                )
            except Exception as e:
                logger.error(f"Agent run failed for case {case_id}: {e}")
                _all_success = False
                end_time = tz_now()
                save_run_results(
                    case_id, "failed", start_time, end_time,
                    (end_time - start_time).total_seconds(),
                    None, None,
                    [{"level": "error", "message": "客户端 Agent 执行过程中发生内部错误，请查看服务端日志获取详情"}],
                    batch_id=batch.id,
                    is_init=info.get("is_init", False),
                )

        _db = SessionLocal()
        try:
            _b = _db.query(db_models.RunBatch).filter(db_models.RunBatch.id == batch.id).first()
            if _b:
                crud._compute_batch_status(_db, _b)
                _db.commit()
        finally:
            _db.close()

        if _all_success:
            try:
                from agent.models import WSMessage, WSMessageType
                session = agent_manager.get_session(agent.id)
                if session:
                    await session.send(WSMessage(
                        type=WSMessageType.SHUTDOWN, agent_id=agent.id,
                    ))
                    logger.info("All cases passed — shutdown signal sent to agent")
                else:
                    logger.info("All cases passed — browser left open for debugging")
            except Exception as exc:
                logger.warning(f"Failed to send shutdown to agent: {exc}")

    _task = _asyncio.create_task(_run_batch())
    def _on_batch_done(t) -> None:
        exc = t.exception()
        if exc:
            logger.error(f"Client agent batch-run task failed: {exc}")
            try:
                from app.database import SessionLocal as _SL
                from app import db_models as _dm
                _db = _SL()
                _b = _db.query(_dm.RunBatch).filter(_dm.RunBatch.id == batch.id).first()
                if _b and _b.status in ("running", "pending"):
                    _b.status = "failed"
                    _b.finished_at = tz_now()
                    _db.commit()
                _db.close()
            except Exception as e:
                logger.warning(f"Failed to mark batch {batch.id} as failed: {e}")
    _task.add_done_callback(_on_batch_done)

    return {
        "message": f"{len(case_ids)} case(s) running on client agent {agent.name}",
        "batch_id": batch.id,
    }
