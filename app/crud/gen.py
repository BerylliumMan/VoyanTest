# app/crud/gen.py - AI 生成会话（GenSession）及其关联表 CRUD
#
# 提供对 GenSession / GenFunctionalPoint / GenTestCase 的纯数据库操作。
# 业务层面的 HTTP 404 抛出与内存清理（``_sessions.pop``）由 router 负责，
# 本文件只关心 SQLAlchemy 查询/写入。
from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app import db_models
# GenTestCaseUpdate 是 gen 子路由的请求体 DTO，目前定义在 routers/gen/schemas.py。
# 这里按 ``update_agent`` 接受 ``models.AgentUpdate`` 的同样模式直接接收 Pydantic 模型，
# 避免在两层各维护一份字段拷贝。后续如需统一迁至 ``app.models.schemas`` 可一并改。
from app.routers.gen.schemas import GenTestCaseUpdate

logger = logging.getLogger(__name__)


# ----------------------------
# GenSession CRUD
# ----------------------------

def list_gen_sessions(
    db: Session,
    page: int,
    page_size: int,
    project_id: int | None = None,
) -> dict:
    """分页获取生成会话列表（按 ``created_at`` 倒序，可按项目筛选）。

    返回 dict: ``{items: list[GenSession], total: int}``
    """
    query = db.query(db_models.GenSession).order_by(db_models.GenSession.created_at.desc())
    if project_id is not None:
        query = query.filter(db_models.GenSession.project_id == project_id)

    total = query.count()
    items = query.offset((page - 1) * page_size).limit(page_size).all()
    return {"items": items, "total": total}


def get_gen_session(db: Session, session_id: str) -> db_models.GenSession | None:
    """通过 ID 获取单个 GenSession，不存在返回 None。"""
    return (
        db.query(db_models.GenSession)
        .filter(db_models.GenSession.id == session_id)
        .first()
    )


def delete_gen_session(db: Session, session_id: str) -> db_models.GenSession | None:
    """删除 GenSession（依赖 ``cascade="all, delete-orphan"`` 联动删除 functional_points / test_cases）。

    返回被删除的 ORM 对象；若 session 不存在返回 None，由 router 决定抛 404。
    """
    record = get_gen_session(db, session_id)
    if record is None:
        return None
    db.delete(record)
    db.commit()
    return record


# ----------------------------
# GenFunctionalPoint CRUD
# ----------------------------

def list_gen_functional_points(db: Session, session_id: str) -> list[db_models.GenFunctionalPoint]:
    """按 ``fp_id`` 升序列出某 session 下的全部功能点。"""
    return (
        db.query(db_models.GenFunctionalPoint)
        .filter(db_models.GenFunctionalPoint.session_id == session_id)
        .order_by(db_models.GenFunctionalPoint.fp_id)
        .all()
    )


# ----------------------------
# GenTestCase CRUD
# ----------------------------

def list_gen_test_cases(db: Session, session_id: str) -> list[db_models.GenTestCase]:
    """按主键 ``id`` 升序列出某 session 下的全部测试用例。"""
    return (
        db.query(db_models.GenTestCase)
        .filter(db_models.GenTestCase.session_id == session_id)
        .order_by(db_models.GenTestCase.id)
        .all()
    )


def get_gen_test_case(
    db: Session,
    session_id: str,
    test_case_id: str,
) -> db_models.GenTestCase | None:
    """通过 ``(session_id, test_case_id)`` 获取单个 GenTestCase，不存在返回 None。"""
    return (
        db.query(db_models.GenTestCase)
        .filter(
            db_models.GenTestCase.session_id == session_id,
            db_models.GenTestCase.test_case_id == test_case_id,
        )
        .first()
    )


def update_gen_test_case(
    db: Session,
    session_id: str,
    test_case_id: str,
    body: GenTestCaseUpdate,
) -> db_models.GenTestCase | None:
    """部分更新 GenTestCase（仅写入 ``body`` 中显式给出的字段）。

    返回更新后的 ORM 对象；若测试用例不存在返回 None，由 router 决定抛 404。
    复制自原 history 路由的 PATCH 行为：仅当字段非 None 时覆盖。
    """
    tc = get_gen_test_case(db, session_id, test_case_id)
    if tc is None:
        return None

    # ``exclude_unset`` 保留 PATCH 语义：未传的字段保持原值
    changes = body.model_dump(exclude_unset=True)
    for key, value in changes.items():
        setattr(tc, key, value)

    db.commit()
    db.refresh(tc)
    return tc


def delete_gen_test_case(
    db: Session,
    session_id: str,
    test_case_id: str,
) -> bool:
    """删除单条 GenTestCase，并把所属 session 的 ``test_cases_count`` 减 1（下限 0）。

    返回 True 表示成功删除；session 或 test_case 不存在则返回 False，由 router 决定抛 404。
    复制自原 history 路由的 DELETE 行为：``max(0, (count or 1) - 1)``。
    """
    record = get_gen_session(db, session_id)
    if record is None:
        return False

    tc = get_gen_test_case(db, session_id, test_case_id)
    if tc is None:
        return False

    db.delete(tc)
    record.test_cases_count = max(0, (record.test_cases_count or 1) - 1)
    db.commit()
    return True
