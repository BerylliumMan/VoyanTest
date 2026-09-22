# app/routers/api_test/import_.py — 接口文档导入（029-api-testing 契约 §1.1）
#
# 同一 URL 同时接受 multipart 上传与 JSON（swagger_url）两种形态：
# 前者由请求的 content-type 分流判定，避免出现两个语义重复的端点。
from __future__ import annotations

import logging
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user, get_user_project_filter
from app.crud import api_definition as crud_api_def
from app.database import get_async_db

logger = logging.getLogger(__name__)

router = APIRouter()

_FETCH_TIMEOUT = 20.0
_MAX_DOC_BYTES = 20 * 1024 * 1024


def _ensure_project_access(user: Any, project_id: int) -> None:
    allowed = get_user_project_filter(user)
    if allowed is not None and project_id not in allowed:
        raise HTTPException(status_code=403, detail="无权访问该项目")


def _to_int(value: Any, field: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail=f"{field} 必须是整数") from None


async def _read_request(request: Request) -> tuple[bytes | None, str, int, int | None, str, str | None]:
    """返回 (raw_bytes, file_name, project_id, module_id, on_conflict, swagger_url)。"""
    content_type = (request.headers.get("content-type") or "").lower()
    if content_type.startswith(("multipart/form-data", "application/x-www-form-urlencoded")):
        form = await request.form()
        upload = form.get("file")
        if upload is None or not getattr(upload, "filename", ""):
            raise HTTPException(status_code=400, detail="缺少上传文件（file 字段）")
        raw = await upload.read()
        file_name = str(upload.filename)
        project_id = _to_int(form.get("project_id"), "project_id")
        module_id_raw = form.get("module_id")
        module_id = _to_int(module_id_raw, "module_id") if module_id_raw not in (None, "", "null") else None
        on_conflict = str(form.get("on_conflict") or "skip")
        return raw, file_name, project_id, module_id, on_conflict, None
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(
            status_code=400, detail="请求体必须是 multipart 文件上传或 JSON"
        ) from None
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="JSON 请求体必须是对象")
    project_id = _to_int(payload.get("project_id"), "project_id")
    module_id = payload.get("module_id")
    module_id = _to_int(module_id, "module_id") if module_id is not None else None
    return (
        None,
        str(payload.get("swagger_url") or ""),
        project_id,
        module_id,
        str(payload.get("on_conflict") or "skip"),
        payload.get("swagger_url"),
    )


async def _fetch_url(url: str) -> bytes:
    url = str(url or "").strip()
    if not url.lower().startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="swagger_url 必须是 http(s) 地址")
    try:
        async with httpx.AsyncClient(timeout=_FETCH_TIMEOUT, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            raw = resp.content
    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            status_code=400, detail=f"下载接口文档失败: HTTP {exc.response.status_code}"
        ) from None
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"下载接口文档失败: {exc}") from None
    if len(raw) > _MAX_DOC_BYTES:
        raise HTTPException(status_code=400, detail="接口文档超过 20MB 上限")
    return raw


@router.post("/import")
async def import_api_document(
    request: Request,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> dict:
    """导入 OpenAPI3 / Swagger2 / Postman v2.1 文档（上传或 URL）。"""
    from app.parsers.api_doc.common import detect_source, parse_document

    raw, file_name, project_id, module_id, on_conflict, swagger_url = await _read_request(request)
    _ensure_project_access(user, project_id)
    if on_conflict not in ("skip", "overwrite"):
        raise HTTPException(status_code=400, detail="on_conflict 只能是 skip 或 overwrite")
    if raw is None:
        raw = await _fetch_url(str(swagger_url or ""))
        file_name = file_name or str(swagger_url)
    if not raw:
        raise HTTPException(status_code=400, detail="接口文档内容为空")

    try:
        source = detect_source(raw)
        parsed = parse_document(raw, file_name=file_name)
    except Exception as exc:
        logger.warning("接口文档解析失败 file=%s: %s", file_name, exc, exc_info=True)
        raise HTTPException(status_code=400, detail=f"接口文档解析失败: {exc}") from None

    operations = [
        {
            "name": op.name,
            "method": op.method,
            "path": op.path,
            "summary": op.summary,
            "operation_id": op.operation_id,
            "tags": op.tags,
            "request_schema": op.request_schema,
            "response_schema": op.response_schema,
            "source": parsed.source,
            "source_ref": file_name,
        }
        for op in parsed.operations
    ]
    result = await crud_api_def.upsert_many(
        db, project_id, operations, module_id=module_id, on_conflict=on_conflict
    )
    warnings = list(parsed.warnings)
    record = await crud_api_def.create_import(
        db,
        {
            "project_id": project_id,
            "file_name": file_name or source,
            "source": source,
            "total_operations": len(parsed.operations),
            "created_count": result["created"],
            "updated_count": result["updated"],
            "skipped_count": result["skipped"],
            "error": "; ".join(result["errors"][:5]) or None,
        },
    )
    return {
        "import_id": record.id,
        "source": source,
        "total_operations": len(parsed.operations),
        "created": result["created"],
        "updated": result["updated"],
        "skipped": result["skipped"],
        "warnings": warnings + result["errors"],
        "operations": result["items"],
    }


@router.get("/imports")
async def list_api_imports(
    project_id: int,
    limit: int = 20,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> dict:
    """导入历史。"""
    _ensure_project_access(user, project_id)
    rows = await crud_api_def.list_imports(db, project_id, limit=limit)
    return {
        "total": len(rows),
        "items": [
            {
                "id": r.id,
                "file_name": r.file_name,
                "source": r.source,
                "total_operations": r.total_operations,
                "created_count": r.created_count,
                "updated_count": r.updated_count,
                "skipped_count": r.skipped_count,
                "error": r.error,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ],
    }
