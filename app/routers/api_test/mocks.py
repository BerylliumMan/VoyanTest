"""接口 Mock：按定义返回固定响应。对外地址不要求登录。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import db_models
from app.auth import get_current_user, get_user_project_filter
from app.crud import api_definition as crud_api_def
from app.database import get_async_db

router = APIRouter()
public_router = APIRouter()


def _ensure_project_access(user, project_id: int) -> None:
    allowed = get_user_project_filter(user)
    if allowed is not None and project_id not in allowed:
        raise HTTPException(status_code=403, detail="无权访问该项目")


def _mock_dict(obj, definition=None) -> dict:
    path = getattr(definition, "path", "") or ""
    return {
        "id": obj.id,
        "project_id": obj.project_id,
        "definition_id": obj.definition_id,
        "name": obj.name,
        "enabled": bool(obj.enabled),
        "status_code": obj.status_code,
        "headers": obj.headers or [],
        "body": obj.body or "",
        "mock_path": f"/mock/{obj.project_id}{path}",
    }


@router.get("/definitions/{definition_id}/mocks")
async def list_mocks(
    definition_id: int,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> dict:
    definition = await crud_api_def.get_api_definition(db, definition_id)
    if definition is None:
        raise HTTPException(status_code=404, detail="接口定义不存在")
    _ensure_project_access(user, definition.project_id)
    rows = (
        await db.execute(
            select(db_models.ApiMock)
            .where(db_models.ApiMock.definition_id == definition_id)
            .order_by(db_models.ApiMock.id.desc())
        )
    ).scalars().all()
    return {"total": len(rows), "items": [_mock_dict(r, definition) for r in rows]}


@router.post("/definitions/{definition_id}/mocks", status_code=201)
async def create_mock(
    definition_id: int,
    payload: dict,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> dict:
    definition = await crud_api_def.get_api_definition(db, definition_id)
    if definition is None:
        raise HTTPException(status_code=404, detail="接口定义不存在")
    _ensure_project_access(user, definition.project_id)
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="请求体必须是对象")
    name = str(payload.get("name") or "").strip() or definition.name
    try:
        status_code = int(payload.get("status_code") or 200)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="status_code 必须是整数") from None
    headers = payload.get("headers") if isinstance(payload.get("headers"), list) else []
    obj = db_models.ApiMock(
        project_id=definition.project_id,
        definition_id=definition.id,
        name=name,
        enabled=bool(payload.get("enabled", True)),
        status_code=status_code,
        headers=headers,
        body="" if payload.get("body") is None else str(payload.get("body")),
    )
    db.add(obj)
    await db.commit()
    await db.refresh(obj)
    return _mock_dict(obj, definition)


@router.delete("/mocks/{mock_id}")
async def delete_mock(
    mock_id: int,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> dict:
    obj = await db.get(db_models.ApiMock, mock_id)
    if obj is None:
        raise HTTPException(status_code=404, detail="Mock 不存在")
    _ensure_project_access(user, obj.project_id)
    await db.delete(obj)
    await db.commit()
    return {"deleted": mock_id}


@public_router.api_route(
    "/mock/{project_id}/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
)
async def serve_mock(
    project_id: int,
    path: str,
    request: Request,
    db: AsyncSession = Depends(get_async_db),
) -> Response:
    """命中第一条启用的 Mock。未命中返回 404，不写测试报告。"""
    wanted = "/" + path.strip("/")
    definitions = (
        await db.execute(
            select(db_models.ApiDefinition).where(
                db_models.ApiDefinition.project_id == project_id,
                db_models.ApiDefinition.method == request.method.upper(),
            )
        )
    ).scalars().all()
    matched = [
        d for d in definitions
        if (d.path or "").rstrip("/") == wanted.rstrip("/")
    ]
    if not matched:
        return Response(
            content='{"detail":"mock not found"}',
            status_code=404,
            media_type="application/json",
        )
    definition_ids = [d.id for d in matched]
    mock = (
        await db.execute(
            select(db_models.ApiMock)
            .where(
                db_models.ApiMock.definition_id.in_(definition_ids),
                db_models.ApiMock.enabled.is_(True),
            )
            .order_by(db_models.ApiMock.id.asc())
        )
    ).scalars().first()
    if mock is None:
        return Response(
            content='{"detail":"mock not found"}',
            status_code=404,
            media_type="application/json",
        )
    headers = {}
    for item in mock.headers or []:
        if isinstance(item, dict) and item.get("key"):
            headers[str(item["key"])] = "" if item.get("value") is None else str(item["value"])
    body = mock.body or ""
    media = headers.get("Content-Type") or headers.get("content-type") or "application/json"
    return Response(content=body, status_code=int(mock.status_code or 200), headers=headers, media_type=media)
