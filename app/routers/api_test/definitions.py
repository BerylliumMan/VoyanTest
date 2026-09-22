# app/routers/api_test/definitions.py — 接口定义 CRUD（029-api-testing 契约 §1.2）
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user, get_user_project_filter
from app.crud import api_definition as crud_api_def
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

    created = await crud_api_def.create_api_definition(
        db,
        {
            "project_id": project_id,
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
        k: payload.get(k) for k in ("name", "summary", "tags", "module_id") if k in payload
    }
    example = payload.get("request_schema", {}).get("example") if isinstance(payload.get("request_schema"), dict) else None
    if example is not None:
        merged = dict(obj.request_schema or {})
        merged["example"] = example
        patch["request_schema"] = merged

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
    await crud_api_def.delete_api_definition(db, definition_id)
    return {"deleted": definition_id}
