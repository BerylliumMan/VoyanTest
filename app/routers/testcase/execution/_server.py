"""服务端执行端点 — 浏览器跑在服务端。

- POST /api/testcases/{case_id}/run         — 单用例
- POST /api/testcases/{case_id}/run-debug   — 调试模式（带暂停+WS广播）
- POST /api/testcases/batch-run             — 批量
- POST /api/testcases/module/{module_id}/run   — 模块下所有用例
- POST /api/testcases/project/{project_id}/run — 项目下所有用例

注意: run_test_case / run_batch_test_cases / run_test_case_in_browser 在函数体内延迟
从 app.routers.testcase.execution 包导入，以便测试 monkeypatch(execution, "run_test_case", ...)
能生效 — 直接 import core.runner 会绕过包级 monkeypatch。
"""
from __future__ import annotations

import asyncio as _asyncio
import logging
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app import crud
from app.auth import get_current_user, get_user_project_filter
from app.database import AsyncSessionLocal, get_async_db
from app.tz import now as tz_now
from app.services.notifications import notify_batch_completed

from ._schemas import BatchRunRequest, DebugRunRequest

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/{case_id}/run")
async def run_test_case_endpoint(
    case_id: int,
    background_tasks: BackgroundTasks,
    user=Depends(get_current_user),
    environment_id: Optional[int] = None,
    db: AsyncSession = Depends(get_async_db),
) -> dict:
    """运行单个测试用例 - 创建单用例批次"""
    db_case = await crud.get_test_case(db, case_id)
    if db_case is None:
        raise HTTPException(status_code=404, detail="Test case not found")

    allowed_ids = get_user_project_filter(user)
    if allowed_ids is not None and db_case.project_id not in allowed_ids:
        raise HTTPException(status_code=404, detail="Test case not found")

    batch = await crud.create_run_batch(db, project_id=db_case.project_id, total_cases=1, triggered_by=getattr(user, 'username', None))

    from core.browser_pool import BrowserPool

    project_id = db_case.project_id
    if await BrowserPool.is_active(project_id):
        from app.routers.testcase import execution as _exec

        async def _run_with_existing_browser() -> None:
            mgr = await BrowserPool.get(project_id)
            if mgr is not None:
                base_url_override = None
                if environment_id:
                    async with AsyncSessionLocal() as _env_db:
                        env = await crud.get_environment(_env_db, environment_id)
                        if env:
                            base_url_override = env.base_url
                await _exec.run_test_case_in_browser(case_id, mgr, batch_id=batch.id, base_url_override=base_url_override)

        background_tasks.add_task(_run_with_existing_browser)
    else:
        from app.routers.testcase import execution as _exec
        background_tasks.add_task(_exec.run_test_case, case_id, batch.id, environment_id=environment_id)

    user_id = getattr(user, "id", None)
    if user_id:
        async def _notify() -> None:
            await notify_batch_completed(batch.id, user_id)
        background_tasks.add_task(_notify)

    return {"id": batch.id, "status": "running", "batch_id": batch.id}


@router.post("/{case_id}/run-debug")
async def run_test_case_debug(
    case_id: int,
    req: DebugRunRequest = DebugRunRequest(),
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> dict:
    """启动调试运行——打开 WebSocket 桥接和暂停模式。"""
    db_case = await crud.get_test_case(db, case_id)
    if db_case is None:
        raise HTTPException(status_code=404, detail="Test case not found")

    allowed_ids = get_user_project_filter(user)
    if allowed_ids is not None and db_case.project_id not in allowed_ids:
        raise HTTPException(status_code=404, detail="Test case not found")

    batch = await crud.create_run_batch(db, project_id=db_case.project_id, total_cases=1, triggered_by=getattr(user, 'username', None))

    run = await crud.create_test_run(
        db, case_id, "running",
        start_time=tz_now(),
        end_time=tz_now(),
        duration=0,
    )

    await db.commit()

    _ = _asyncio.create_task(
        _run_debug_mode(case_id, batch.id, run.id, req.environment_id)
    )

    return {
        "batch_id": batch.id,
        "run_id": run.id,
        "case_id": case_id,
        "status": "debug_running",
        "message": "调试模式已启动，请通过 WebSocket 连接 /ws/logs/{} 接收实时事件".format(run.id),
    }


async def _run_debug_mode(case_id: int, batch_id: int, run_id: int, environment_id: Optional[int] = None):
    """在后台运行调试模式，通过 run_id 建立 WS 桥接。"""
    from app.routers.testcase import execution as _exec

    try:
        _ = await _exec.run_test_case(
            case_id=case_id,
            batch_id=batch_id,
            environment_id=environment_id,
            debug_mode=True,
        )
        from app.websocket import LogBroadcaster
        await LogBroadcaster.remove_pause_event(run_id)
    except Exception as exc:
        logger.exception("Debug run failed")
        try:
            from app.websocket import LogBroadcaster
            await LogBroadcaster.log_run_complete(run_id, status="error", error=str(exc))
        except Exception:
            pass


@router.post("/batch-run")
async def batch_run_cases(req: BatchRunRequest, background_tasks: BackgroundTasks, user=Depends(get_current_user), db: AsyncSession = Depends(get_async_db)) -> dict:
    """批量运行选中的测试用例 - 创建批次"""
    if not req.case_ids:
        raise HTTPException(status_code=400, detail="No cases selected")

    async def _load_test_cases(cids: list[int]) -> list:
        result = []
        for cid in cids:
            tc = await crud.get_test_case(db, cid)
            if tc:
                result.append(tc)
        return result

    test_cases = await _load_test_cases(req.case_ids)

    if not test_cases:
        raise HTTPException(status_code=404, detail="No valid test cases found")

    allowed_ids = get_user_project_filter(user)
    project_id = test_cases[0].project_id
    if allowed_ids is not None and project_id not in allowed_ids:
        raise HTTPException(status_code=404, detail="No valid test cases found")

    case_ids = [c.id for c in test_cases]
    init_case_ids = req.init_case_ids or []

    if init_case_ids:
        init_tcs = await _load_test_cases(init_case_ids)
        for tc in init_tcs:
            if tc.project_id != project_id:
                raise HTTPException(status_code=400, detail=f"Init case {tc.id} is not in the same project")

    total = len(case_ids) + len(init_case_ids)
    batch = await crud.create_run_batch(db, project_id=project_id, total_cases=total, triggered_by=getattr(user, 'username', None))
    from app.routers.testcase import execution as _exec
    background_tasks.add_task(
        _exec.run_batch_test_cases,
        case_ids,
        project_id,
        batch_id=batch.id,
        environment_id=req.environment_id,
        init_case_ids=init_case_ids,
    )

    user_id = getattr(user, "id", None)
    if user_id:
        background_tasks.add_task(notify_batch_completed, batch.id, user_id)

    return {"batch_id": batch.id, "total": total, "started": total, "status": "running"}


@router.post("/module/{module_id}/run")
async def run_module_test_cases(
    module_id: int,
    background_tasks: BackgroundTasks,
    user=Depends(get_current_user),
    environment_id: Optional[int] = None,
    db: AsyncSession = Depends(get_async_db),
) -> dict:
    """运行模块下所有测试用例 - 创建批次"""
    db_module = await crud.get_module(db, module_id)
    if db_module is None:
        raise HTTPException(status_code=404, detail="Module not found")

    allowed_ids = get_user_project_filter(user)
    if allowed_ids is not None and db_module.project_id not in allowed_ids:
        raise HTTPException(status_code=404, detail="Module not found")

    test_cases = await crud.get_all_test_cases_for_module(db, module_id)
    if not test_cases:
        return {"message": f"No test cases found for module {module_id} to run."}

    case_ids = [c.id for c in test_cases]
    project_id = db_module.project_id

    batch = await crud.create_run_batch(db, project_id=project_id, total_cases=len(case_ids), triggered_by=getattr(user, 'username', None))

    from app.routers.testcase import execution as _exec
    background_tasks.add_task(
        _exec.run_batch_test_cases,
        case_ids,
        project_id,
        batch_id=batch.id,
        environment_id=environment_id,
    )

    return {"batch_id": batch.id, "total": len(test_cases), "started": len(test_cases), "status": "running"}


@router.post("/project/{project_id}/run")
async def run_project_test_cases(
    project_id: int,
    background_tasks: BackgroundTasks,
    user=Depends(get_current_user),
    environment_id: Optional[int] = None,
    db: AsyncSession = Depends(get_async_db),
) -> dict:
    """运行项目下所有测试用例 - 创建批次"""
    db_project = await crud.get_project(db, project_id)
    if db_project is None:
        raise HTTPException(status_code=404, detail="Project not found")

    allowed_ids = get_user_project_filter(user)
    if allowed_ids is not None and project_id not in allowed_ids:
        raise HTTPException(status_code=404, detail="Project not found")

    test_cases = await crud.get_all_test_cases_for_project(db, project_id)
    if not test_cases:
        return {"message": f"No test cases found for project {project_id} to run."}

    case_ids = [c.id for c in test_cases]

    batch = await crud.create_run_batch(db, project_id=project_id, total_cases=len(case_ids), triggered_by=getattr(user, 'username', None))

    from app.routers.testcase import execution as _exec
    background_tasks.add_task(
        _exec.run_batch_test_cases,
        case_ids,
        project_id,
        batch_id=batch.id,
        environment_id=environment_id,
    )

    return {"batch_id": batch.id, "total": len(test_cases), "started": len(test_cases), "status": "running"}
