# app/crud/api_dataset.py — 接口数据集 CRUD + CSV 解析（029-api-testing T044）
#
# 行形状统一为「字典行」：rows = [{"列": "值"}]。CSV 导入（csv.DictReader）与手工
# 创建共用 validate_dataset_fields 校验；执行器按行注入 dataset 作用域（T045）。
from __future__ import annotations

import csv
import io
import logging
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import db_models

logger = logging.getLogger(__name__)

MAX_CSV_BYTES = 2 * 1024 * 1024  # CSV 文件大小上限 2MB
MAX_DATASET_ROWS = 1000  # 数据集行数上限（CSV 与手工一致）
DEFAULT_DATASET_NAME = "未命名数据集"


class DatasetValidationError(ValueError):
    """数据集字段 / CSV 内容不可恢复问题（路由层转 400）。"""


def validate_dataset_fields(columns: Any, rows: Any) -> list[str]:
    """校验 columns/rows，返回规范化后的 columns（去首尾空白）。

    规则：columns 非空 list[str] 且无空名/重名；rows 为 list[dict]，每行 key 集合
    必须等于 columns；行数不得超过 MAX_DATASET_ROWS。
    """
    if not isinstance(columns, list) or not columns:
        raise DatasetValidationError("columns 必须是非空数组")
    cleaned: list[str] = []
    for column in columns:
        if not isinstance(column, str) or not column.strip():
            raise DatasetValidationError(f"columns 含非法列名: {column!r}（必须是非空字符串）")
        cleaned.append(column.strip())
    duplicates = sorted({c for c in cleaned if cleaned.count(c) > 1})
    if duplicates:
        raise DatasetValidationError(f"columns 存在重复列名: {', '.join(duplicates)}")
    if not isinstance(rows, list):
        raise DatasetValidationError("rows 必须是数组")
    if len(rows) > MAX_DATASET_ROWS:
        raise DatasetValidationError(f"rows 超出上限（最多 {MAX_DATASET_ROWS} 行）")
    expected = set(cleaned)
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise DatasetValidationError(f"rows[{index}] 必须是对象（{{列: 值}}）")
        keys = set(row.keys())
        if keys != expected:
            missing = sorted(expected - keys)
            extra = sorted(str(k) for k in keys - expected)
            raise DatasetValidationError(
                f"rows[{index}] 的列与 columns 不一致（缺: {missing}，多: {extra}）"
            )
    return cleaned


def parse_csv_dataset(content: bytes) -> dict:
    """CSV 字节 → ``{"columns": [...], "rows": [{列: 值}]}``；非法内容抛可读错误。

    - ``utf-8-sig`` 兼容 BOM；非 UTF-8 → DatasetValidationError
    - 短行缺失单元格补空串；多出的单元格（restkey=None）视为列不一致报错
    - 空文件/无表头、重复/空列名、>2MB、>1000 行均报错
    """
    if len(content) > MAX_CSV_BYTES:
        raise DatasetValidationError(
            f"CSV 文件过大（{len(content)} 字节，上限 {MAX_CSV_BYTES} 字节）"
        )
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise DatasetValidationError("CSV 编码必须是 UTF-8（支持 BOM）") from exc

    reader = csv.DictReader(io.StringIO(text))
    columns = [str(name).strip() for name in (reader.fieldnames or [])]
    if not columns:
        raise DatasetValidationError("CSV 文件为空或缺少表头")
    if any(not name for name in columns):
        raise DatasetValidationError("CSV 表头存在空列名")
    duplicates = sorted({c for c in columns if columns.count(c) > 1})
    if duplicates:
        raise DatasetValidationError(f"CSV 表头存在重复列名: {', '.join(duplicates)}")

    rows: list[dict[str, str]] = []
    for raw in reader:
        if None in raw:
            raise DatasetValidationError(
                f"CSV 第 {reader.line_num} 行列数多于表头（{len(raw)} > {len(columns)}）"
            )
        if len(rows) >= MAX_DATASET_ROWS:
            raise DatasetValidationError(
                f"CSV 数据行数超出上限（最多 {MAX_DATASET_ROWS} 行）"
            )
        rows.append(
            {
                column: ("" if raw.get(column) is None else str(raw.get(column)))
                for column in columns
            }
        )
    return {"columns": columns, "rows": rows}


async def list_datasets(
    db: AsyncSession,
    project_id: int,
    *,
    limit: int = 200,
    offset: int = 0,
) -> list[db_models.ApiDataset]:
    """列出项目内数据集（新的在前）。"""
    stmt = (
        select(db_models.ApiDataset)
        .where(db_models.ApiDataset.project_id == project_id)
        .order_by(db_models.ApiDataset.id.desc())
        .limit(max(1, min(limit, 1000)))
        .offset(max(0, offset))
    )
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def get_dataset(db: AsyncSession, dataset_id: int) -> Optional[db_models.ApiDataset]:
    result = await db.execute(
        select(db_models.ApiDataset).where(db_models.ApiDataset.id == dataset_id)
    )
    return result.scalar_one_or_none()


async def create_dataset(db: AsyncSession, data: dict) -> db_models.ApiDataset:
    """创建数据集（columns/rows 不一致抛 DatasetValidationError）。"""
    rows = list(data.get("rows") or [])
    columns = validate_dataset_fields(data.get("columns") or [], rows)
    obj = db_models.ApiDataset(
        project_id=data["project_id"],
        name=str(data.get("name") or "").strip() or DEFAULT_DATASET_NAME,
        columns=columns,
        rows=rows,
        source=str(data.get("source") or "manual"),
    )
    db.add(obj)
    await db.commit()
    await db.refresh(obj)
    return obj


async def update_dataset(
    db: AsyncSession, dataset_id: int, patch: dict
) -> Optional[db_models.ApiDataset]:
    """更新 name/columns/rows/source；仅当 columns/rows 被提供时才重新校验。"""
    obj = await get_dataset(db, dataset_id)
    if obj is None:
        return None
    if patch.get("name") is not None:
        obj.name = str(patch["name"]).strip() or obj.name
    if "columns" in patch or "rows" in patch:
        columns = patch.get("columns") if "columns" in patch else obj.columns
        rows = patch.get("rows") if "rows" in patch else obj.rows
        obj.columns = validate_dataset_fields(columns, rows)
        obj.rows = list(rows)
    if patch.get("source"):
        obj.source = str(patch["source"])
    await db.commit()
    await db.refresh(obj)
    return obj


async def delete_dataset(db: AsyncSession, dataset_id: int) -> bool:
    obj = await get_dataset(db, dataset_id)
    if obj is None:
        return False
    await db.delete(obj)
    await db.commit()
    return True


__all__ = [
    "MAX_CSV_BYTES",
    "MAX_DATASET_ROWS",
    "DEFAULT_DATASET_NAME",
    "DatasetValidationError",
    "validate_dataset_fields",
    "parse_csv_dataset",
    "list_datasets",
    "get_dataset",
    "create_dataset",
    "update_dataset",
    "delete_dataset",
]
