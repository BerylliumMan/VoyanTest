# app/routers/api_test/scenarios.py — 接口测试场景 CRUD + 执行（029-api-testing 场景页）
#
# 场景 = 有序的接口用例集合 + 执行环境 + 场景级变量覆盖。
# 执行复用批次/报告链路（创建命名批次 → 逐条 run_api_case_server → 计数/暂停/通知），
# 报告页、暂停/停止、通知与普通批量执行一致。
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app import crud
from app.auth import get_current_user, get_user_project_filter
from app.crud import api_scenario as crud_scenario
from app.crud.api_scenario import ScenarioValidationError
from app.database import AsyncSessionLocal, get_async_db

logger = logging.getLogger(__name__)

router = APIRouter()


def _ensure_project_access(user: Any, project_id: int) -> None:
    allowed = get_user_project_filter(user)
    if allowed is not None and project_id not in allowed:
        raise HTTPException(status_code=403, detail="无权访问该项目")


async def _environment_name(db: AsyncSession, environment_id: Any) -> str | None:
    if environment_id is None:
        return None
    try:
        env = await crud.get_environment(db, int(environment_id))
    except Exception:
        return None
    return getattr(env, "name", None)


async def _resolve_cases(db: AsyncSession, steps: list[dict]) -> list[dict]:
    """把 steps 解析成带用例名的明细（已被删除的用例标 missing=True）。"""
    out: list[dict] = []
    for step in steps or []:
        entry = {
            "case_id": step.get("case_id"),
            "enabled": bool(step.get("enabled", True)),
            "order": step.get("order"),
            "name": None,
            "missing": False,
        }
        tc = await crud.get_test_case(db, step.get("case_id"))
        if tc is None:
            entry["missing"] = True
        else:
            entry["name"] = getattr(tc, "name", None)
        out.append(entry)
    return out


def _summarize(obj) -> dict:
    steps = obj.steps or []
    enabled = [s for s in steps if isinstance(s, dict) and s.get("enabled", True)]
    return {
        "id": obj.id,
        "project_id": obj.project_id,
        "name": obj.name,
        "description": obj.description,
        "environment_id": obj.environment_id,
        "environment_name": None,  # 列表接口按需填充
        "step_count": len(steps),
        "enabled_count": len(enabled),
        "updated_at": obj.updated_at.isoformat() if obj.updated_at else None,
    }


@router.get("/scenarios")
async def list_scenarios(
    project_id: int,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> dict:
    """项目内场景列表。"""
    _ensure_project_access(user, project_id)
    objs = await crud_scenario.list_scenarios(db, project_id)
    items = []
    for obj in objs:
        item = _summarize(obj)
        item["environment_name"] = await _environment_name(db, obj.environment_id)
        items.append(item)
    return {"total": len(items), "items": items}


@router.post("/scenarios", status_code=201)
async def create_scenario(
    payload: dict,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> dict:
    """创建场景（steps 内的用例必须存在、同项目且为接口用例，否则 400）。"""
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="请求体必须是对象")
    project_id = payload.get("project_id")
    if not isinstance(project_id, int):
        raise HTTPException(status_code=400, detail="project_id 必须是整数")
    _ensure_project_access(user, project_id)
    try:
        obj = await crud_scenario.create_scenario(
            db,
            {
                "project_id": project_id,
                "name": payload.get("name"),
                "description": payload.get("description"),
                "environment_id": payload.get("environment_id"),
                "variables": payload.get("variables"),
                "steps": payload.get("steps"),
            },
        )
    except ScenarioValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return await scenario_detail(obj.id, user, db)


@router.get("/scenarios/{scenario_id}")
async def scenario_detail(
    scenario_id: int,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> dict:
    """场景明细（含解析后的用例名与环境名）。"""
    obj = await crud_scenario.get_scenario(db, scenario_id)
    if obj is None:
        raise HTTPException(status_code=404, detail="场景不存在")
    _ensure_project_access(user, obj.project_id)
    item = _summarize(obj)
    item["environment_name"] = await _environment_name(db, obj.environment_id)
    item["variables"] = obj.variables or []
    item["cases"] = await _resolve_cases(db, obj.steps or [])
    return item


@router.put("/scenarios/{scenario_id}")
async def update_scenario(
    scenario_id: int,
    payload: dict,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> dict:
    """局部更新场景。"""
    obj = await crud_scenario.get_scenario(db, scenario_id)
    if obj is None:
        raise HTTPException(status_code=404, detail="场景不存在")
    _ensure_project_access(user, obj.project_id)
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="请求体必须是对象")
    patch = {
        k: payload.get(k)
        for k in ("name", "description", "environment_id", "variables", "steps")
        if k in payload
    }
    try:
        updated = await crud_scenario.update_scenario(db, scenario_id, patch)
    except ScenarioValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return await scenario_detail(updated.id, user, db)


@router.delete("/scenarios/{scenario_id}", status_code=204)
async def delete_scenario(
    scenario_id: int,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> None:
    """删除场景（不影响其引用的用例与历史批次）。"""
    obj = await crud_scenario.get_scenario(db, scenario_id)
    if obj is None:
        return None
    _ensure_project_access(user, obj.project_id)
    await crud_scenario.delete_scenario(db, scenario_id)
    return None


@router.post("/scenarios/{scenario_id}/run")
async def run_scenario(
    scenario_id: int,
    background_tasks: BackgroundTasks,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> dict:
    """执行场景：创建命名批次，按顺序逐条执行启用的用例。

    复用批次/报告链路：报告页、暂停/停止、完成通知与普通批量执行一致。
    """
    from core.runner._api_execution import run_api_case_server

    obj = await crud_scenario.get_scenario(db, scenario_id)
    if obj is None:
        raise HTTPException(status_code=404, detail="场景不存在")
    _ensure_project_access(user, obj.project_id)

    enabled = [
        s for s in (obj.steps or []) if isinstance(s, dict) and s.get("enabled", True)
    ]
    if not enabled:
        raise HTTPException(status_code=400, detail="场景内没有启用的用例")
    case_ids = [int(s["case_id"]) for s in enabled]

    scenario_vars: list[dict] = list(obj.variables or [])
    environment_id = obj.environment_id
    project_id = obj.project_id
    batch = await crud.create_run_batch(
        db,
        project_id,
        name=f"场景：{obj.name}",
        total_cases=len(case_ids),
        triggered_by=getattr(user, "username", None),
    )
    batch_id = batch.id
    user_id = getattr(user, "id", None)

    async def _run() -> None:
        from app import execution_control
        from app.database import AsyncSessionLocal as _SessionLocal
        from app.services.notifications import notify_batch_completed

        try:
            async with _SessionLocal() as session:
                for case_id in case_ids:
                    await execution_control.wait_if_paused(batch_id)
                    if execution_control.is_stopped(batch_id):
                        logger.info("场景执行被停止 scenario=%s batch=%s", scenario_id, batch_id)
                        break
                    await run_api_case_server(
                        case_id,
                        db=session,
                        batch_id=batch_id,
                        environment_id=environment_id,
                        case_variables=scenario_vars,
                    )
        except Exception:
            logger.exception("场景执行异常 scenario=%s batch=%s", scenario_id, batch_id)
        finally:
            # 停止/异常导致的提前退出：把批次置为 cancelled，避免永久 running
            try:
                async with _SessionLocal() as session:
                    from app import db_models
                    from sqlalchemy import select as _select

                    result = await session.execute(
                        _select(db_models.RunBatch).where(db_models.RunBatch.id == batch_id)
                    )
                    b = result.scalar_one_or_none()
                    if b is not None and b.status == "running":
                        done = (b.passed or 0) + (b.failed or 0)
                        if done < (b.total_cases or 0):
                            b.status = "cancelled"
                            await session.commit()
            except Exception:
                logger.exception("场景批次收尾失败 batch=%s", batch_id)
            if user_id:
                try:
                    await notify_batch_completed(batch_id, user_id)
                except Exception:
                    logger.exception("场景批次通知失败 batch=%s", batch_id)

    background_tasks.add_task(_run)
    return {"batch_id": batch_id, "total": len(case_ids), "started": len(case_ids), "status": "running"}
