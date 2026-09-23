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


async def _case_api_info(db: AsyncSession, case) -> dict:
    """用例对应的接口：优先用绑定的接口定义，否则用请求快照里的方法和地址。"""
    from app.crud import api_definition as crud_api_def

    spec = getattr(case, "api_spec", None) or {}
    raw_steps = spec.get("steps") if isinstance(spec, dict) else None
    first = raw_steps[0] if isinstance(raw_steps, list) and raw_steps and isinstance(raw_steps[0], dict) else {}
    request = first.get("request") if isinstance(first.get("request"), dict) else {}
    def_id = getattr(case, "api_definition_id", None) or first.get("definition_id")
    method = str(request.get("method") or "").upper()
    path = ""
    definition_name = None
    if def_id is not None:
        try:
            definition = await crud_api_def.get_api_definition(db, int(def_id))
        except (TypeError, ValueError):
            definition = None
        if definition is not None:
            method = str(definition.method or method).upper()
            path = definition.path or ""
            definition_name = definition.name
    if not path:
        url = str(request.get("url") or "")
        path = url.replace("{{baseUrl}}", "") or url
    return {"method": method, "path": path, "definition_name": definition_name}


async def _resolve_steps(db: AsyncSession, steps: list[dict]) -> list[dict]:
    """给步骤补上展示名。旧数据没有 type 时按用例步骤处理。"""
    out: list[dict] = []
    for step in steps or []:
        if not isinstance(step, dict):
            continue
        entry = dict(step)
        entry["enabled"] = bool(step.get("enabled", True))
        step_type = step.get("type") or ("case" if step.get("case_id") is not None else "request")
        entry["type"] = step_type
        entry["missing"] = False
        if step_type == "case":
            tc = await crud.get_test_case(db, step.get("case_id"))
            if tc is None:
                entry["missing"] = True
                entry["name"] = None
            else:
                entry["name"] = getattr(tc, "name", None)
                entry.update(await _case_api_info(db, tc))
        elif step_type == "definition":
            from app.crud import api_definition as crud_api_def

            definition = await crud_api_def.get_api_definition(db, step.get("definition_id"))
            if definition is None:
                entry["missing"] = True
            else:
                entry["definition_name"] = definition.name
                entry["method"] = definition.method
                entry["path"] = definition.path
                if not entry.get("name"):
                    entry["name"] = definition.name
        children = step.get("children")
        if isinstance(children, list) and children:
            entry["children"] = await _resolve_steps(db, children)
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
        "share_cookie": bool(getattr(obj, "share_cookie", False)),
        "continue_on_failure": bool(getattr(obj, "continue_on_failure", False)),
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
                "share_cookie": payload.get("share_cookie", False),
                "continue_on_failure": payload.get("continue_on_failure", False),
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
    resolved = await _resolve_steps(db, obj.steps or [])
    item["steps"] = resolved
    item["cases"] = [s for s in resolved if s.get("type") == "case"]
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
        for k in (
            "name",
            "description",
            "environment_id",
            "variables",
            "steps",
            "share_cookie",
            "continue_on_failure",
        )
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
    """执行场景：创建命名批次，按启用步骤顺序执行。

    复用批次/报告链路：报告页、暂停/停止、完成通知与普通批量执行一致。
    步骤间共享本次提取出的变量；share_cookie 为真时共用一个 HTTP 客户端。
    """
    from core.runner._api_execution import run_scenario_batch

    obj = await crud_scenario.get_scenario(db, scenario_id)
    if obj is None:
        raise HTTPException(status_code=404, detail="场景不存在")
    _ensure_project_access(user, obj.project_id)

    enabled = [
        s for s in (obj.steps or []) if isinstance(s, dict) and s.get("enabled", True)
    ]
    if not enabled:
        raise HTTPException(status_code=400, detail="场景内没有启用的步骤")

    batch = await crud.create_run_batch(
        db,
        obj.project_id,
        name=f"场景：{obj.name}",
        total_cases=len(enabled),
        triggered_by=getattr(user, "username", None),
        source="api_scenario",
        source_id=obj.id,
    )
    batch_id = batch.id
    user_id = getattr(user, "id", None)
    background_tasks.add_task(run_scenario_batch, scenario_id, batch_id, user_id)
    return {"batch_id": batch_id, "total": len(enabled), "started": len(enabled), "status": "running"}
