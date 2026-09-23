# app/routers/api_test/definitions.py — 接口定义 CRUD（029-api-testing 契约 §1.2）
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from sqlalchemy import select

from app import db_models, models
from app.auth import get_current_user, get_user_project_filter
from app.crud import api_definition as crud_api_def
from app.crud.module import get_module
from app.crud.testcase import create_test_case
from app.database import get_async_db

logger = logging.getLogger(__name__)

router = APIRouter()


def _ensure_project_access(user: Any, project_id: int) -> None:
    allowed = get_user_project_filter(user)
    if allowed is not None and project_id not in allowed:
        raise HTTPException(status_code=403, detail="无权访问该项目")


def _serialize(obj) -> dict:
    return {
        "id": obj.id,
        "project_id": obj.project_id,
        "module_id": obj.module_id,
        "name": obj.name,
        "method": obj.method,
        "path": obj.path,
        "summary": obj.summary,
        "tags": obj.tags,
        "operation_id": obj.operation_id,
        "source": obj.source,
        "request_schema": obj.request_schema or {},
        "response_schema": obj.response_schema or {},
        "updated_at": obj.updated_at.isoformat() if obj.updated_at else None,
    }


@router.get("/definitions")
async def list_definitions(
    project_id: int,
    module_id: int | None = None,
    method: str | None = None,
    keyword: str | None = None,
    page: int = 1,
    page_size: int = 50,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> dict:
    """接口定义列表（分页）。"""
    _ensure_project_access(user, project_id)
    page = max(1, page)
    page_size = max(1, min(page_size, 200))
    rows = await crud_api_def.list_api_definitions(
        db,
        project_id,
        module_id=module_id,
        keyword=keyword,
        source=None,
        limit=page_size,
        offset=(page - 1) * page_size,
    )
    if method:
        rows = [r for r in rows if (r.method or "").upper() == method.upper()]
    total = await crud_api_def.count_api_definitions(db, project_id)
    return {"total": total, "page": page, "page_size": page_size, "items": [_serialize(r) for r in rows]}


@router.get("/definitions/{definition_id}")
async def get_definition(
    definition_id: int,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> dict:
    """接口定义详情（含 request_schema / response_schema）。"""
    obj = await crud_api_def.get_api_definition(db, definition_id)
    if obj is None:
        raise HTTPException(status_code=404, detail="接口定义不存在")
    _ensure_project_access(user, obj.project_id)
    return _serialize(obj)


@router.post("/definitions", status_code=201)
async def create_definition(
    payload: dict,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> dict:
    """手动新增接口定义（不依赖文档导入）。

    冲突规则：同项目 method+path 已存在 → 409（避免同一接口两份定义各自生成用例）。
    """
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="请求体必须是对象")
    project_id = payload.get("project_id")
    if not isinstance(project_id, int):
        raise HTTPException(status_code=400, detail="project_id 必须是整数")
    _ensure_project_access(user, project_id)

    name = str(payload.get("name") or "").strip()
    method = str(payload.get("method") or "GET").strip().upper()
    path = str(payload.get("path") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="接口名称不能为空")
    if not path:
        raise HTTPException(status_code=400, detail="接口路径不能为空")
    if not path.startswith("/"):
        raise HTTPException(status_code=400, detail="接口路径必须以 / 开头")
    if method not in ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"):
        raise HTTPException(status_code=400, detail=f"不支持的 HTTP 方法: {method}")

    existing = await crud_api_def.get_by_key(db, project_id, method, path)
    if existing is not None:
        raise HTTPException(status_code=409, detail=f"同项目已存在 {method} {path}（可在右侧直接编辑它）")

    module_id = payload.get("module_id")
    if module_id in ("", "null"):
        module_id = None
    if module_id is not None:
        try:
            module_id = int(module_id)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="module_id 必须是整数") from None
        module = await get_module(db, module_id)
        if module is None or module.project_id != project_id:
            raise HTTPException(status_code=400, detail="分组不存在或不属于该项目")

    created = await crud_api_def.create_api_definition(
        db,
        {
            "project_id": project_id,
            "module_id": module_id,
            "name": name,
            "method": method,
            "path": path,
            "summary": payload.get("summary"),
            "tags": payload.get("tags"),
            "source": "manual",
            "request_schema": payload.get("request_schema") or {},
        },
    )
    return _serialize(created)


@router.post("/definitions/{definition_id}/copy", status_code=201)
async def copy_definition(
    definition_id: int,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> dict:
    """复制接口定义：名称加「（副本）」后缀、路径追加 `-copy` 后缀，
    冲突时自动递增（`-copy2`…），分组（module_id）与请求/响应 schema 一并复制。
    """
    obj = await crud_api_def.get_api_definition(db, definition_id)
    if obj is None:
        raise HTTPException(status_code=404, detail="接口定义不存在")
    _ensure_project_access(user, obj.project_id)

    base_name = obj.name or f"{obj.method} {obj.path}"
    base_path = obj.path or "/"
    name, path, n = f"{base_name}（副本）", f"{base_path}-copy", 1
    while await crud_api_def.get_by_key(db, obj.project_id, obj.method, path) is not None:
        n += 1
        name, path = f"{base_name}（副本{n}）", f"{base_path}-copy{n}"
        if n > 99:  # 防御：几乎不可能到达
            raise HTTPException(status_code=409, detail="副本过多，请先清理")

    created = await crud_api_def.create_api_definition(
        db,
        {
            "project_id": obj.project_id,
            "module_id": obj.module_id,
            "name": name,
            "method": obj.method,
            "path": path,
            "summary": obj.summary,
            "tags": obj.tags,
            "source": "manual",
            "request_schema": dict(obj.request_schema or {}),
            "response_schema": dict(obj.response_schema or {}),
        },
    )
    return _serialize(created)


@router.put("/definitions/{definition_id}")
async def update_definition(
    definition_id: int,
    payload: dict,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> dict:
    """更新接口定义（name/summary/tags/module_id + request_schema 的 example）。"""
    obj = await crud_api_def.get_api_definition(db, definition_id)
    if obj is None:
        raise HTTPException(status_code=404, detail="接口定义不存在")
    _ensure_project_access(user, obj.project_id)
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="请求体必须是对象")

    patch = {
        k: payload.get(k)
        for k in ("name", "summary", "tags", "module_id", "method", "path")
        if k in payload
    }
    if isinstance(payload.get("request_schema"), dict):
        incoming = payload["request_schema"]
        if "example" in incoming and set(incoming) <= {"example"}:
            merged = dict(obj.request_schema or {})
            merged["example"] = incoming.get("example")
            patch["request_schema"] = merged
        else:
            patch["request_schema"] = incoming

    updated = await crud_api_def.update_api_definition(db, definition_id, patch)
    return _serialize(updated)


@router.delete("/definitions/{definition_id}")
async def delete_definition(
    definition_id: int,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> dict:
    """删除接口定义（已生成的用例不受影响——用例里是 api_spec 快照）。"""
    obj = await crud_api_def.get_api_definition(db, definition_id)
    if obj is None:
        raise HTTPException(status_code=404, detail="接口定义不存在")
    _ensure_project_access(user, obj.project_id)
    from app.crud.api_refs import scenarios_using_definition

    linked = await db.execute(
        select(db_models.TestCase.id).where(
            db_models.TestCase.project_id == obj.project_id,
            db_models.TestCase.case_kind == "api",
            db_models.TestCase.api_definition_id == definition_id,
        )
    )
    case_ids = {int(row[0]) for row in linked.all()}
    names = await scenarios_using_definition(db, obj.project_id, definition_id, case_ids)
    if names:
        raise HTTPException(status_code=400, detail="仍被场景引用：" + "、".join(names))
    await crud_api_def.delete_api_definition(db, definition_id)
    return {"deleted": definition_id}


def _linked_to_definition(case, definition_id: int) -> bool:
    if getattr(case, "api_definition_id", None) == definition_id:
        return True
    spec = getattr(case, "api_spec", None) or {}
    steps = spec.get("steps") if isinstance(spec, dict) else None
    if not steps or not isinstance(steps[0], dict):
        return False
    try:
        return int(steps[0].get("definition_id")) == definition_id
    except (TypeError, ValueError):
        return False


def _case_brief(case, definition_version: int | None = None) -> dict:
    saved = getattr(case, "definition_version", None)
    stale = saved is not None and definition_version is not None and int(saved) < int(definition_version)
    return {
        "id": case.id,
        "name": case.name,
        "priority": case.priority,
        "api_definition_id": getattr(case, "api_definition_id", None),
        "definition_version": saved,
        "current_version": definition_version,
        "stale": stale,
        "updated_at": case.updated_at.isoformat() if case.updated_at else None,
    }


@router.get("/definitions/{definition_id}/cases")
async def list_definition_cases(
    definition_id: int,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> dict:
    """该接口定义下的用例（含仅在 api_spec 里记下 definition_id 的旧用例）。"""
    obj = await crud_api_def.get_api_definition(db, definition_id)
    if obj is None:
        raise HTTPException(status_code=404, detail="接口定义不存在")
    _ensure_project_access(user, obj.project_id)
    result = await db.execute(
        select(db_models.TestCase)
        .where(
            db_models.TestCase.project_id == obj.project_id,
            db_models.TestCase.case_kind == "api",
        )
        .order_by(db_models.TestCase.id.desc())
    )
    items = [
        _case_brief(c, obj.version)
        for c in result.scalars().all()
        if _linked_to_definition(c, definition_id)
    ]
    return {"total": len(items), "items": items}


@router.post("/definitions/{definition_id}/cases", status_code=201)
async def create_definition_case(
    definition_id: int,
    payload: dict,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> dict:
    """把当前调试快照存成挂在该定义上的接口用例。调试接口本身不落库。"""
    obj = await crud_api_def.get_api_definition(db, definition_id)
    if obj is None:
        raise HTTPException(status_code=404, detail="接口定义不存在")
    _ensure_project_access(user, obj.project_id)
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="请求体必须是对象")
    name = str(payload.get("name") or "").strip() or obj.name or f"{obj.method} {obj.path}"
    spec = payload.get("api_spec")
    if not isinstance(spec, dict):
        spec = {
            "schema_version": 1,
            "variables": [],
            "fail_policy": "fail_fast",
            "steps": [
                {
                    "order": 1,
                    "name": name,
                    "enable": True,
                    "definition_id": definition_id,
                    "request": {
                        "method": obj.method,
                        "url": "{{baseUrl}}" + (obj.path or "/"),
                        "headers": [],
                        "query": [],
                        "body": {"type": "none", "content": ""},
                        "auth": {"type": "none"},
                        "timeout_ms": 30000,
                        "follow_redirects": True,
                        "verify_ssl": True,
                    },
                    "assertions": [],
                    "extractors": [],
                    "pre": [],
                }
            ],
        }
    steps = spec.get("steps")
    if isinstance(steps, list) and steps and isinstance(steps[0], dict):
        if steps[0].get("definition_id") in (None, ""):
            steps[0]["definition_id"] = definition_id
        req = steps[0].get("request") if isinstance(steps[0].get("request"), dict) else {}
        if not str(req.get("method") or "").strip():
            req["method"] = obj.method
        if not str(req.get("url") or "").strip():
            req["url"] = "{{baseUrl}}" + (obj.path or "/")
        steps[0]["request"] = req
        spec["steps"] = steps
    created = await create_test_case(
        db,
        models.TestCaseCreate(
            project_id=obj.project_id,
            module_id=obj.module_id,
            name=name,
            description=str(payload.get("description") or ""),
            case_kind="api",
            api_spec=spec,
            api_definition_id=definition_id,
            definition_version=int(obj.version or 1),
            priority=str(payload.get("priority") or "medium"),
            tags=payload.get("tags"),
            steps=[],
        ),
    )
    return _case_brief(created, obj.version)


@router.get("/definitions/{definition_id}/references")
async def definition_references(
    definition_id: int,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> dict:
    """这个接口被哪些用例和场景使用。"""
    from app.crud.api_refs import scenarios_using_definition

    obj = await crud_api_def.get_api_definition(db, definition_id)
    if obj is None:
        raise HTTPException(status_code=404, detail="接口定义不存在")
    _ensure_project_access(user, obj.project_id)
    result = await db.execute(
        select(db_models.TestCase).where(
            db_models.TestCase.project_id == obj.project_id,
            db_models.TestCase.case_kind == "api",
        )
    )
    cases = [c for c in result.scalars().all() if _linked_to_definition(c, definition_id)]
    case_ids = {c.id for c in cases}
    scenarios = await scenarios_using_definition(db, obj.project_id, definition_id, case_ids)
    return {
        "cases": [{"id": c.id, "name": c.name} for c in cases],
        "scenarios": scenarios,
    }


@router.get("/cases/{case_id}/references")
async def case_references(
    case_id: int,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> dict:
    """这个用例被哪些场景使用。"""
    from app.crud.api_refs import scenarios_using_case

    case = await db.get(db_models.TestCase, case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="用例不存在")
    _ensure_project_access(user, case.project_id)
    return {"scenarios": await scenarios_using_case(db, case.project_id, case_id)}


@router.post("/definitions/{definition_id}/cases/{case_id}/sync")
async def sync_definition_case(
    definition_id: int,
    case_id: int,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> dict:
    """把定义上的 method、path、query、body 结构同步到用例。断言和提取不动。"""
    from app.crud.api_refs import apply_definition_to_spec

    obj = await crud_api_def.get_api_definition(db, definition_id)
    if obj is None:
        raise HTTPException(status_code=404, detail="接口定义不存在")
    _ensure_project_access(user, obj.project_id)
    case = await db.get(db_models.TestCase, case_id)
    if case is None or not _linked_to_definition(case, definition_id):
        raise HTTPException(status_code=404, detail="用例不存在或不属于该接口")
    old_path = None
    steps = (case.api_spec or {}).get("steps") if isinstance(case.api_spec, dict) else None
    if steps and isinstance(steps[0], dict):
        url = str((steps[0].get("request") or {}).get("url") or "")
        if obj.path and obj.path not in url:
            old_path = None
    case.api_spec = apply_definition_to_spec(case.api_spec or {}, obj, old_path)
    case.definition_version = int(obj.version or 1)
    case.api_definition_id = definition_id
    await db.commit()
    await db.refresh(case)
    return _case_brief(case, obj.version)
