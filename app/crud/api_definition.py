# app/crud/api_definition.py — 接口定义 / 导入记录 CRUD（029-api-testing）
#
# 判重键：(project_id, method, path)。同一键再次导入时按 on_conflict 决定
# skip（跳过）或 overwrite（覆盖 request/response schema 与元信息）。
import logging

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import db_models

logger = logging.getLogger(__name__)

EXISTING_KEYS = ("created", "updated", "skipped")


async def list_api_definitions(
    db: AsyncSession,
    project_id: int,
    *,
    module_id: int | None = None,
    keyword: str | None = None,
    source: str | None = None,
    limit: int = 200,
    offset: int = 0,
) -> list[db_models.ApiDefinition]:
    """列出接口定义（项目内），支持模块/关键字/来源过滤。"""
    stmt = select(db_models.ApiDefinition).where(
        db_models.ApiDefinition.project_id == project_id
    )
    if module_id is not None:
        stmt = stmt.where(db_models.ApiDefinition.module_id == module_id)
    if source:
        stmt = stmt.where(db_models.ApiDefinition.source == source)
    if keyword:
        like = f"%{keyword}%"
        stmt = stmt.where(
            db_models.ApiDefinition.name.ilike(like)
            | db_models.ApiDefinition.path.ilike(like)
        )
    stmt = stmt.order_by(db_models.ApiDefinition.path, db_models.ApiDefinition.method)
    stmt = stmt.limit(max(1, min(limit, 1000))).offset(max(0, offset))
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def count_api_definitions(db: AsyncSession, project_id: int) -> int:
    result = await db.execute(
        select(func.count()).select_from(db_models.ApiDefinition).where(
            db_models.ApiDefinition.project_id == project_id
        )
    )
    return int(result.scalar() or 0)


async def get_api_definition(
    db: AsyncSession, definition_id: int
) -> db_models.ApiDefinition | None:
    result = await db.execute(
        select(db_models.ApiDefinition).where(db_models.ApiDefinition.id == definition_id)
    )
    return result.scalar_one_or_none()


async def get_by_key(
    db: AsyncSession, project_id: int, method: str, path: str
) -> db_models.ApiDefinition | None:
    result = await db.execute(
        select(db_models.ApiDefinition).where(
            db_models.ApiDefinition.project_id == project_id,
            db_models.ApiDefinition.method == method,
            db_models.ApiDefinition.path == path,
        )
    )
    return result.scalar_one_or_none()


def _apply_operation_fields(obj: db_models.ApiDefinition, data: dict) -> None:
    for field in (
        "name",
        "summary",
        "tags",
        "operation_id",
        "module_id",
        "request_schema",
        "response_schema",
        "source",
        "source_ref",
    ):
        if field in data and data[field] is not None:
            setattr(obj, field, data[field])


async def create_api_definition(db: AsyncSession, data: dict) -> db_models.ApiDefinition:
    """创建接口定义（同 project/method/path 已存在时抛 ValueError）。"""
    existing = await get_by_key(db, data["project_id"], data["method"], data["path"])
    if existing is not None:
        raise ValueError(
            f"接口已存在: {data['method']} {data['path']}（id={existing.id}）"
        )
    obj = db_models.ApiDefinition(
        project_id=data["project_id"],
        module_id=data.get("module_id"),
        name=data.get("name") or f"{data['method']} {data['path']}",
        method=data["method"],
        path=data["path"],
        protocol=data.get("protocol") or "http",
        summary=data.get("summary") or "",
        operation_id=data.get("operation_id") or "",
        request_schema=data.get("request_schema") or {},
        response_schema=data.get("response_schema") or {},
        source=data.get("source") or "manual",
        source_ref=data.get("source_ref"),
        tags=data.get("tags") or "",
    )
    db.add(obj)
    await db.commit()
    await db.refresh(obj)
    return obj


async def upsert_api_definition(
    db: AsyncSession, data: dict, *, on_conflict: str = "skip"
) -> tuple[db_models.ApiDefinition, str]:
    """按 (project_id, method, path) 判重写入；返回 (对象, created|updated|skipped)。"""
    existing = await get_by_key(db, data["project_id"], data["method"], data["path"])
    if existing is None:
        return await create_api_definition(db, data), "created"
    if on_conflict != "overwrite":
        return existing, "skipped"
    _apply_operation_fields(existing, data)
    existing.version = int(existing.version or 1) + 1
    await db.commit()
    await db.refresh(existing)
    return existing, "updated"


async def upsert_many(
    db: AsyncSession,
    project_id: int,
    operations: list[dict],
    *,
    module_id: int | None = None,
    on_conflict: str = "skip",
) -> dict:
    """批量写入解析结果；逐条失败不中断（errors 里记录原因）。"""
    counts = {k: 0 for k in EXISTING_KEYS}
    items: list[dict] = []
    errors: list[str] = []
    for op in operations:
        payload = {
            **op,
            "project_id": project_id,
            "module_id": module_id if module_id is not None else op.get("module_id"),
        }
        method = str(payload.get("method") or "").upper()
        path = str(payload.get("path") or "")
        if not method or not path:
            errors.append(f"缺少 method/path，已跳过: {op.get('name')!r}")
            continue
        payload["method"] = method
        payload["path"] = path
        try:
            obj, action = await upsert_api_definition(db, payload, on_conflict=on_conflict)
        except Exception as exc:  # 单条失败不影响整批导入
            logger.warning("接口导入失败 %s %s: %s", method, path, exc, exc_info=True)
            errors.append(f"{method} {path}: {exc}")
            continue
        counts[action] += 1
        items.append(
            {"id": obj.id, "name": obj.name, "method": obj.method, "path": obj.path, "action": action}
        )
    return {**counts, "items": items, "errors": errors}


async def update_api_definition(
    db: AsyncSession, definition_id: int, patch: dict
) -> db_models.ApiDefinition | None:
    obj = await get_api_definition(db, definition_id)
    if obj is None:
        return None
    for field in ("name", "summary", "tags"):
        if patch.get(field) is not None:
            setattr(obj, field, patch[field])
    # module_id 允许**显式置空**：把接口移出分组到「未分组」是合法操作
    # （早期实现统一跳过 None，导致 module_id:null 静默失效）
    if "module_id" in patch:
        setattr(obj, "module_id", patch["module_id"])
    await db.commit()
    await db.refresh(obj)
    return obj


async def delete_api_definition(db: AsyncSession, definition_id: int) -> bool:
    obj = await get_api_definition(db, definition_id)
    if obj is None:
        return False
    await db.delete(obj)
    await db.commit()
    return True


async def create_import(db: AsyncSession, data: dict) -> db_models.ApiImport:
    obj = db_models.ApiImport(
        project_id=data["project_id"],
        file_name=data.get("file_name") or "",
        source=data.get("source") or "unknown",
        total_operations=int(data.get("total_operations") or 0),
        created_count=int(data.get("created_count") or 0),
        updated_count=int(data.get("updated_count") or 0),
        skipped_count=int(data.get("skipped_count") or 0),
        error=data.get("error"),
    )
    db.add(obj)
    await db.commit()
    await db.refresh(obj)
    return obj


async def list_imports(
    db: AsyncSession, project_id: int, limit: int = 20
) -> list[db_models.ApiImport]:
    result = await db.execute(
        select(db_models.ApiImport)
        .where(db_models.ApiImport.project_id == project_id)
        .order_by(db_models.ApiImport.id.desc())
        .limit(max(1, min(limit, 100)))
    )
    return list(result.scalars().all())


__all__ = [
    "list_api_definitions",
    "count_api_definitions",
    "get_api_definition",
    "get_by_key",
    "create_api_definition",
    "upsert_api_definition",
    "upsert_many",
    "update_api_definition",
    "delete_api_definition",
    "create_import",
    "list_imports",
]
