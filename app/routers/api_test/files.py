# app/routers/api_test/files.py — 测试文件端点（031-api-testing-enhancements T015）
#
# 契约（contracts/api-enhancements.md §1.7）：
#   POST   /api/api-test/files            上传（multipart/form-data: file + project_id 可选）→ 201 / 413
#   GET    /api/api-test/files?project_id 列表（含 ref_count）
#   DELETE /api/api-test/files/{id}       删除；被引用 409（`?force=true` 二次确认）
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.crud import api_file as crud_file
from app.database import get_async_db
from app.models.schemas import TestFileOut

logger = logging.getLogger(__name__)

router = APIRouter(tags=["api-test-files"])


@router.post("/files", response_model=TestFileOut, status_code=201)
async def upload_test_file(
    file: UploadFile = File(..., description="要托管的测试文件（≤ api_test_file_max_mb）"),
    project_id: Optional[int] = Form(default=None),
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> TestFileOut:
    """上传测试文件（浏览器 → 服务端专用目录，卷持久化）。"""
    content = await file.read()
    try:
        row = await crud_file.create_test_file(
            db,
            name=file.filename or "file.bin",
            content=content,
            content_type=file.content_type,
            project_id=project_id,
            uploaded_by=getattr(user, "id", None),
        )
    except crud_file.ApiFileTooLarge as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except crud_file.ApiFileError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception:
        logger.exception("测试文件上传失败")
        raise HTTPException(status_code=400, detail="测试文件上传失败") from None

    return TestFileOut(
        id=row.id,
        name=row.name,
        size=row.size,
        content_type=row.content_type,
        project_id=row.project_id,
        ref_count=0,
        created_at=row.created_at,
    )


@router.get("/files")
async def list_test_files(
    project_id: Optional[int] = Query(default=None),
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> dict:
    """测试文件列表（含被引用次数 ref_count）。"""
    items = await crud_file.list_test_files(db, project_id=project_id)
    return {"total": len(items), "items": items}


@router.delete("/files/{file_id}", status_code=204)
async def delete_test_file(
    file_id: int,
    force: bool = Query(default=False, description="被引用时二次确认"),
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> None:
    """删除测试文件；被用例引用且未 force → 409（提示引用数）。"""
    try:
        await crud_file.delete_test_file(db, file_id, force=force)
    except crud_file.ApiFileInUse as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except crud_file.ApiFileError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
