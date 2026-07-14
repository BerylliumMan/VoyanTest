# app/crud/user.py - 用户 CRUD
#
# 提供对 User 表的纯数据库操作。密码哈希、密码强度校验、审计日志等
# 业务逻辑由 router/auth 层负责，本文件只关心 SQLAlchemy 查询/写入。
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import db_models

logger = logging.getLogger(__name__)


# ----------------------------
# User CRUD
# ----------------------------

async def list_users(db: AsyncSession) -> list[db_models.User]:
    """获取所有用户（按 created_at 倒序）。"""
    result = await db.execute(
        select(db_models.User).order_by(db_models.User.created_at.desc())
    )
    return result.scalars().all()


async def get_user_by_username(db: AsyncSession, username: str) -> db_models.User | None:
    """通过用户名获取用户（用户名在调用方已做 lowercase + strip）。"""
    result = await db.execute(
        select(db_models.User).where(db_models.User.username == username)
    )
    return result.scalar_one_or_none()


async def get_user_by_id(db: AsyncSession, user_id: int) -> db_models.User | None:
    """通过 ID 获取用户。"""
    result = await db.execute(
        select(db_models.User).where(db_models.User.id == user_id)
    )
    return result.scalar_one_or_none()


async def get_users_by_ids(db: AsyncSession, user_ids: list[int]) -> list[db_models.User]:
    """批量通过 ID 获取用户（IN 查询），用于审计日志关联用户名等场景。"""
    if not user_ids:
        return []
    result = await db.execute(
        select(db_models.User).where(db_models.User.id.in_(user_ids))
    )
    return result.scalars().all()


async def create_user(
    db: AsyncSession,
    username: str,
    password_hash: str,
    role: str,
    must_change_password: bool,
    project_ids: list[int] | None = None,
) -> db_models.User:
    """创建新用户（已哈希的密码），初始 must_change_password=True。"""
    user = db_models.User(
        username=username,
        password_hash=password_hash,
        role=role,
        must_change_password=must_change_password,
        project_ids=project_ids,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def update_user_fields(
    db: AsyncSession,
    user_id: int,
    role: str | None = None,
    status: str | None = None,
    project_ids: list[int] | None = None,
    password_hash: str | None = None,
    must_change_password: bool | None = None,
    login_attempts: int | None = None,
    locked_until = None,
    last_login_at = None,
    nickname: str | None = None,
    email: str | None = None,
) -> db_models.User | None:
    """部分更新 User 字段（仅更新非 None 的入参字段）。

    返回更新后的 ORM 对象；若 User 不存在返回 None，由 router 决定抛 404。
    复制自原 user_router.py 的更新行为：业务校验（角色/状态取值）由 router 负责。
    """
    user = await get_user_by_id(db, user_id)
    if user is None:
        return None

    if role is not None:
        user.role = role
    if status is not None:
        user.status = status
    if project_ids is not None:
        user.project_ids = project_ids
    if password_hash is not None:
        user.password_hash = password_hash
    if must_change_password is not None:
        user.must_change_password = must_change_password
    if login_attempts is not None:
        user.login_attempts = login_attempts
    if locked_until is not None:
        user.locked_until = locked_until
    if last_login_at is not None:
        user.last_login_at = last_login_at
    if nickname is not None:
        user.nickname = nickname
    if email is not None:
        user.email = email

    await db.commit()
    await db.refresh(user)
    return user


async def unlock_user_if_expired(db: AsyncSession, user: db_models.User) -> db_models.User:
    """若用户处于 locked 状态但锁定时间已过期，则自动解锁并重置 login_attempts。

    修改 user 后直接 commit + refresh，返回最新的 user 对象。
    不存在需要锁定的场景返回原 user。
    复制自 auth_router._login_user 中的解锁分支。
    """
    if user.status == "locked" and user.locked_until:
        from app.tz import now as tz_now
        if user.locked_until <= tz_now():
            user.status = "active"
            user.locked_until = None
            user.login_attempts = 0
            await db.commit()
            await db.refresh(user)
    return user


async def commit_user(db: AsyncSession) -> None:
    """仅 commit（用于 user 对象在 router 中已就地修改的最小迁移场景）。

    例如 auth_router 的登录失败计数：先对 user.login_attempts 累加，
    再 commit_user(db) 持久化。后续如需更细粒度控制可改为 update_user_fields。
    """
    await db.commit()
