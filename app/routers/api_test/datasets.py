# app/routers/api_test/datasets.py — 数据集 CRUD + CSV 导入（029-api-testing 契约 §1.7 T044）
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user, get_user_project_filter
from app.crud import api_dataset as crud_dataset
from app.crud.api_dataset import DatasetValidationError
from app.database import get_async_db
from app.models.schemas import ApiDatasetPayload

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
        "name": obj.name,
        "columns": obj.columns or [],
        "rows": obj.rows or [],
        "source": obj.source,
        "updated_at": obj.updated_at.isoformat() if obj.updated_at else None,
    }


@router.get("/datasets")
async def list_datasets(
    project_id: int,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> dict:
    """项目内数据集列表。"""
    _ensure_project_access(user, project_id)
    items = await crud_dataset.list_datasets(db, project_id)
    return {"total": len(items), "items": [_serialize(d) for d in items]}


@router.post("/datasets")
async def create_dataset(
    payload: ApiDatasetPayload,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> dict:
    """创建数据集（columns/rows 不一致 → 400）。"""
    _ensure_project_access(user, payload.project_id)
    try:
        obj = await crud_dataset.create_dataset(db, payload.model_dump())
    except DatasetValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _serialize(obj)


@router.put("/datasets/{dataset_id}")
async def update_dataset(
    dataset_id: int,
    payload: dict,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> dict:
    """更新数据集（name/columns/rows/source）。"""
    obj = await crud_dataset.get_dataset(db, dataset_id)
    if obj is None:
        raise HTTPException(status_code=404, detail="数据集不存在")
    _ensure_project_access(user, obj.project_id)
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="请求体必须是对象")
    try:
        updated = await crud_dataset.update_dataset(db, dataset_id, payload)
    except DatasetValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _serialize(updated)


@router.delete("/datasets/{dataset_id}")
async def delete_dataset(
    dataset_id: int,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> dict:
    """删除数据集。"""
    obj = await crud_dataset.get_dataset(db, dataset_id)
    if obj is None:
        raise HTTPException(status_code=404, detail="数据集不存在")
    _ensure_project_access(user, obj.project_id)
    await crud_dataset.delete_dataset(db, dataset_id)
    return {"deleted": dataset_id}


@router.post("/datasets/import")
async def import_datasets(
    file: UploadFile = File(...),
    user=Depends(get_current_user),
) -> dict:
    """CSV → ``{columns, rows}``（只解析不落库，前端确认后再 POST /datasets 保存）。

    上限：文件 2MB、数据行 1000；空文件/缺表头/列不一致 → 400。
    """
    content = await file.read(crud_dataset.MAX_CSV_BYTES + 1)
    try:
        return crud_dataset.parse_csv_dataset(content)
    except DatasetValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
