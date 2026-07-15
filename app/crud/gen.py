# app/crud/gen.py - AI 生成会话（GenSession）及其关联表 CRUD
#
# 提供对 GenSession / GenFunctionalPoint / GenTestCase 的纯数据库操作。
# 业务层面的 HTTP 404 抛出与内存清理（``_sessions.pop``）由 router 负责，
# 本文件只关心 SQLAlchemy 查询/写入。
from __future__ import annotations

import logging

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import db_models
# GenTestCaseUpdate 是 gen 子路由的请求体 DTO，目前定义在 routers/gen/schemas.py。
# 这里按 ``update_agent`` 接受 ``models.AgentUpdate`` 的同样模式直接接收 Pydantic 模型，
# 避免在两层各维护一份字段拷贝。后续如需统一迁至 ``app.models.schemas`` 可一并改。
from app.routers.gen.schemas import GenTestCaseUpdate

logger = logging.getLogger(__name__)


# ----------------------------
# GenSession CRUD
# ----------------------------

async def list_gen_sessions(
    db: AsyncSession,
    page: int,
    page_size: int,
    project_id: int | None = None,
    user_id_filter: int | None = None,
) -> dict:
    """分页获取生成会话列表（按 ``created_at`` 倒序，可按项目筛选、用户隔离）。

    返回 dict: ``{items: list[GenSession], total: int}``
    """
    from sqlalchemy.orm import selectinload

    items_stmt = (
        select(db_models.GenSession)
        .options(selectinload(db_models.GenSession.project))
        .order_by(db_models.GenSession.created_at.desc())
    )
    if project_id is not None:
        items_stmt = items_stmt.where(db_models.GenSession.project_id == project_id)
    if user_id_filter is not None:
        items_stmt = items_stmt.where(db_models.GenSession.user_id == user_id_filter)

    count_stmt = select(func.count()).select_from(db_models.GenSession)
    if project_id is not None:
        count_stmt = count_stmt.where(db_models.GenSession.project_id == project_id)
    if user_id_filter is not None:
        count_stmt = count_stmt.where(db_models.GenSession.user_id == user_id_filter)
    total = (await db.execute(count_stmt)).scalar()

    items_stmt = items_stmt.offset((page - 1) * page_size).limit(page_size)
    items = (await db.execute(items_stmt)).scalars().all()
    return {"items": items, "total": total}


async def get_gen_session(db: AsyncSession, session_id: str) -> db_models.GenSession | None:
    """通过 ID 获取单个 GenSession，不存在返回 None。"""
    result = await db.execute(
        select(db_models.GenSession).where(db_models.GenSession.id == session_id)
    )
    return result.scalar_one_or_none()


async def create_gen_session(
    db: AsyncSession,
    session_id: str,
    filename: str,
    filenames: str,
    project_id: int | None,
    project_description: str,
    status: str = "analyzing",
    user_id: int | None = None,
) -> db_models.GenSession:
    """创建一条 GenSession 行（路由层 upload 用）。

    ``filenames`` 应为 JSON 字符串（与原路由一致）；router 负责序列化。
    """
    record = db_models.GenSession(
        id=session_id,
        filename=filename,
        filenames=filenames,
        project_id=project_id,
        user_id=user_id,
        project_description=project_description,
        status=status,
    )
    db.add(record)
    await db.commit()
    return record


async def increment_imported_count(
    db: AsyncSession,
    session_id: str,
    project_id: int,
    increment: int,
) -> db_models.GenSession | None:
    """把 GenSession.imported_count 增加 ``increment``，并把空 project_id 回填为 ``project_id``。

    复制自原 import_routes 行为：``(imported_count or 0) + len(created)``，
    且 ``record.project_id is None`` 时回填。返回更新后的 ORM 对象；
    record 不存在返回 None。
    """
    record = await get_gen_session(db, session_id)
    if record is None:
        return None
    record.imported_count = (record.imported_count or 0) + increment
    if record.project_id is None:
        record.project_id = project_id
    await db.commit()
    return record


async def update_gen_session_status(
    db: AsyncSession,
    session_id: str,
    status: str,
    error_message: str = "",
    functional_points_count: int | None = None,
    test_cases_count: int | None = None,
    completed_at=None,
) -> db_models.GenSession | None:
    """更新 GenSession 的运行状态、错误信息和计数（用于 upload 后台线程的 finalize）。

    ``completed_at`` 由 router 显式传入（来自 ``datetime.now()``），None 表示不更新。
    若需写入 functional_points / test_cases，调用方应在 commit 前自行 ``db.add(...)`` 关联记录。
    返回更新后的 ORM 对象；record 不存在返回 None。
    """
    record = await get_gen_session(db, session_id)
    if record is None:
        return None

    record.status = status
    record.error_message = error_message
    if functional_points_count is not None:
        record.functional_points_count = functional_points_count
    if test_cases_count is not None:
        record.test_cases_count = test_cases_count
    if completed_at is not None:
        record.completed_at = completed_at

    await db.commit()
    return record


async def persist_gen_session_results(
    db: AsyncSession,
    session_id: str,
    status: str,
    error_message: str,
    functional_points_count: int,
    test_cases_count: int,
    completed_at,
    functional_points: list | None = None,
    test_cases: list | None = None,
) -> db_models.GenSession | None:
    """完整 finalize GenSession：更新状态/计数并把 functional_points / test_cases 写入子表。

    复制自原 upload._update_db_session 行为（status == "completed" 时写入子表并 commit），
    是 router 一次性调用的事务封装。返回更新后的 ORM 对象；record 不存在返回 None。
    """
    record = await get_gen_session(db, session_id)
    if record is None:
        return None

    record.status = status
    record.error_message = error_message
    record.functional_points_count = functional_points_count
    record.test_cases_count = test_cases_count
    if completed_at is not None:
        record.completed_at = completed_at

    if status == "completed" and functional_points and test_cases:
        for fp in functional_points:
            db.add(db_models.GenFunctionalPoint(
                session_id=session_id,
                fp_id=fp.id,
                module=fp.module,
                name=fp.name,
                description=fp.description,
                category=fp.category,
            ))
        for tc in test_cases:
            db.add(db_models.GenTestCase(
                session_id=session_id,
                test_case_id=tc.test_case_id,
                module=tc.module,
                title=tc.title,
                preconditions=tc.preconditions,
                test_steps=tc.test_steps,
                expected_result=tc.expected_result,
                priority=tc.priority,
            ))

    await db.commit()
    return record


async def delete_gen_session(db: AsyncSession, session_id: str) -> db_models.GenSession | None:
    """删除 GenSession（依赖 ``cascade="all, delete-orphan"`` 联动删除 functional_points / test_cases）。

    返回被删除的 ORM 对象；若 session 不存在返回 None，由 router 决定抛 404。
    """
    record = await get_gen_session(db, session_id)
    if record is None:
        return None
    await db.delete(record)
    await db.commit()
    return record


# ----------------------------
# GenFunctionalPoint CRUD
# ----------------------------

async def list_gen_functional_points(db: AsyncSession, session_id: str) -> list[db_models.GenFunctionalPoint]:
    """按 ``fp_id`` 升序列出某 session 下的全部功能点。"""
    result = await db.execute(
        select(db_models.GenFunctionalPoint)
        .where(db_models.GenFunctionalPoint.session_id == session_id)
        .order_by(db_models.GenFunctionalPoint.fp_id)
    )
    return result.scalars().all()


# ----------------------------
# GenTestCase CRUD
# ----------------------------

async def list_gen_test_cases(db: AsyncSession, session_id: str) -> list[db_models.GenTestCase]:
    """按主键 ``id`` 升序列出某 session 下的全部测试用例。"""
    result = await db.execute(
        select(db_models.GenTestCase)
        .where(db_models.GenTestCase.session_id == session_id)
        .order_by(db_models.GenTestCase.id)
    )
    return result.scalars().all()


async def get_gen_test_case(
    db: AsyncSession,
    session_id: str,
    test_case_id: str,
) -> db_models.GenTestCase | None:
    """通过 ``(session_id, test_case_id)`` 获取单个 GenTestCase，不存在返回 None。"""
    result = await db.execute(
        select(db_models.GenTestCase).where(
            db_models.GenTestCase.session_id == session_id,
            db_models.GenTestCase.test_case_id == test_case_id,
        )
    )
    return result.scalar_one_or_none()


async def update_gen_test_case(
    db: AsyncSession,
    session_id: str,
    test_case_id: str,
    body: GenTestCaseUpdate,
) -> db_models.GenTestCase | None:
    """部分更新 GenTestCase（仅写入 ``body`` 中显式给出的字段）。

    返回更新后的 ORM 对象；若测试用例不存在返回 None，由 router 决定抛 404。
    复制自原 history 路由的 PATCH 行为：仅当字段非 None 时覆盖。
    """
    tc = await get_gen_test_case(db, session_id, test_case_id)
    if tc is None:
        return None

    # ``exclude_unset`` 保留 PATCH 语义：未传的字段保持原值
    changes = body.model_dump(exclude_unset=True)
    for key, value in changes.items():
        setattr(tc, key, value)

    await db.commit()
    await db.refresh(tc)
    return tc


async def delete_gen_test_case(
    db: AsyncSession,
    session_id: str,
    test_case_id: str,
) -> bool:
    """删除单条 GenTestCase，并把所属 session 的 ``test_cases_count`` 减 1（下限 0）。

    返回 True 表示成功删除；session 或 test_case 不存在则返回 False，由 router 决定抛 404。
    复制自原 history 路由的 DELETE 行为：``max(0, (count or 1) - 1)``。
    """
    record = await get_gen_session(db, session_id)
    if record is None:
        return False

    tc = await get_gen_test_case(db, session_id, test_case_id)
    if tc is None:
        return False

    await db.delete(tc)
    record.test_cases_count = max(0, (record.test_cases_count or 1) - 1)
    await db.commit()
    return True
