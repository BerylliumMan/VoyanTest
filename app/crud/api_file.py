# app/crud/api_file.py — 测试文件 CRUD（031-api-testing-enhancements T014）
#
# 平台托管的 multipart 夹具：浏览器上传 → 服务端专用目录（卷持久化）→ 用例以
# `platform://<file_id>` 引用（见 core/api_spec.py 的 multipart 校验）。
# 关键约束：
#   · 单文件上限读 app/config.py::api_test_file_max_mb（宪法：Settings 唯一来源）
#   · 删除被引用文件时必须 force（路由层转 409），避免"删了文件用例全挂"
#   · 引用计数按 test_cases.api_spec 中的 platform://<id> 出现次数统计（去重到用例级）
from __future__ import annotations

import logging
import re
import uuid
from pathlib import Path
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import db_models
from app.config import get_settings
from core.api_runner.request_builder import parse_platform_ref  # 单一来源（core 层解析）

logger = logging.getLogger(__name__)

PLATFORM_REF_PREFIX = "platform://"
_ANY_PLATFORM_REF_RE = re.compile(r"platform://(\d+)")
_UNSAFE_NAME_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


class ApiFileError(ValueError):
    """测试文件相关不可恢复问题（路由层转 400/413/409）。"""


class ApiFileTooLarge(ApiFileError):
    """超过 api_test_file_max_mb 上限（路由层转 413）。"""


class ApiFileInUse(ApiFileError):
    """文件仍被用例引用（路由层转 409，可 force 覆盖）。"""


def _max_bytes() -> int:
    return max(1, int(get_settings().api_test_file_max_mb)) * 1024 * 1024


def storage_root() -> Path:
    """测试文件存储根目录（Config.api_test_file_dir；部署挂载 /app/data）。"""
    root = Path(get_settings().api_test_file_dir)
    root.mkdir(parents=True, exist_ok=True)
    return root


def _safe_name(name: str) -> str:
    base = Path(str(name or "").strip()).name or "file.bin"
    cleaned = _UNSAFE_NAME_RE.sub("_", base).strip() or "file.bin"
    return cleaned[:120]


def file_abs_path(row: db_models.ApiTestFile) -> Path:
    return Path(row.storage_path)


async def create_test_file(
    db: AsyncSession,
    *,
    name: str,
    content: bytes,
    content_type: Optional[str] = None,
    project_id: Optional[int] = None,
    uploaded_by: Optional[int] = None,
    storage_root_override: Optional[Path] = None,
) -> db_models.ApiTestFile:
    """落盘 + 落库。超限抛 ApiFileTooLarge；空文件抛 ApiFileError。"""
    payload = content or b""
    size = len(payload)
    if size <= 0:
        raise ApiFileError("文件内容为空")
    limit = _max_bytes()
    if size > limit:
        raise ApiFileTooLarge(
            f"文件超过上限 {limit // (1024 * 1024)}MB（实际 {size} 字节）"
        )
    root = Path(storage_root_override) if storage_root_override else storage_root()
    sub = root / (str(project_id) if project_id else "_global")
    sub.mkdir(parents=True, exist_ok=True)
    target = sub / f"{uuid.uuid4().hex}_{_safe_name(name)}"
    target.write_bytes(payload)

    row = db_models.ApiTestFile(
        name=str(name or target.name),
        size=size,
        content_type=(content_type or None),
        storage_path=str(target),
        project_id=project_id,
        uploaded_by=uploaded_by,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def get_test_file(db: AsyncSession, file_id: int) -> Optional[db_models.ApiTestFile]:
    return await db.get(db_models.ApiTestFile, file_id)


def iter_platform_refs(spec: Any) -> set[int]:
    """从 api_spec 中提取全部 platform://<id> 引用（执行前解析测试文件用）。"""
    if not spec:
        return set()
    try:
        import json

        text = json.dumps(spec, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = str(spec)
    return {int(m) for m in _ANY_PLATFORM_REF_RE.findall(text)}


async def _reference_counts(db: AsyncSession, file_ids: list[int]) -> dict[int, int]:
    """按用例统计引用次数（同一条用例多次引用只计 1）。"""
    if not file_ids:
        return {}
    wanted = set(file_ids)
    counts: dict[int, int] = {fid: 0 for fid in wanted}
    stmt = select(db_models.TestCase.id, db_models.TestCase.api_spec).where(
        db_models.TestCase.api_spec.is_not(None)
    )
    for _, spec in (await db.execute(stmt)).all():
        for fid in iter_platform_refs(spec) & wanted:
            counts[fid] = counts.get(fid, 0) + 1
    return counts


async def count_references(db: AsyncSession, file_id: int) -> int:
    return (await _reference_counts(db, [file_id])).get(file_id, 0)


async def list_test_files(
    db: AsyncSession, project_id: Optional[int] = None
) -> list[dict]:
    """列表（含 ref_count）；project_id 为空时返回全部（管理员场景）。"""
    stmt = select(db_models.ApiTestFile).order_by(db_models.ApiTestFile.id.desc())
    if project_id is not None:
        stmt = stmt.where(db_models.ApiTestFile.project_id == project_id)
    rows = list((await db.execute(stmt)).scalars().all())
    counts = await _reference_counts(db, [r.id for r in rows])
    return [
        {
            "id": r.id,
            "name": r.name,
            "size": r.size,
            "content_type": r.content_type,
            "project_id": r.project_id,
            "ref_count": counts.get(r.id, 0),
            "created_at": r.created_at,
        }
        for r in rows
    ]


async def delete_test_file(db: AsyncSession, file_id: int, *, force: bool = False) -> None:
    """删除元数据与磁盘文件；被引用且未 force 时抛 ApiFileInUse。"""
    row = await get_test_file(db, file_id)
    if row is None:
        raise ApiFileError("文件不存在")
    refs = await count_references(db, file_id)
    if refs and not force:
        raise ApiFileInUse(f"有 {refs} 条用例引用该文件")
    path = Path(row.storage_path)
    try:
        if path.exists():
            path.unlink()
    except OSError as exc:  # 磁盘删除失败不应阻断元数据清理
        logger.warning("删除测试文件磁盘副本失败: %s", exc, exc_info=True)
    await db.delete(row)
    await db.commit()


async def resolve_platform_paths(
    db: AsyncSession, file_ids: set[int]
) -> dict[int, str]:
    """platform 文件 id → 服务端绝对路径（执行层打开文件用）。"""
    wanted = {int(fid) for fid in file_ids if fid}
    if not wanted:
        return {}
    stmt = select(db_models.ApiTestFile).where(db_models.ApiTestFile.id.in_(wanted))
    rows = list((await db.execute(stmt)).scalars().all())
    return {r.id: r.storage_path for r in rows}
