# app/crud/environment.py - 环境 CRUD
import logging

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app import db_models, models
from app.crud.project import get_project

logger = logging.getLogger(__name__)


# ----------------------------
# 环境 CRUD
# ----------------------------

async def get_environments(db: AsyncSession, project_id: int) -> list[db_models.Environment]:
    """获取项目的所有环境"""
    result = await db.execute(
        select(db_models.Environment)
        .where(db_models.Environment.project_id == project_id)
        .order_by(db_models.Environment.created_at.asc())
    )
    return result.scalars().all()


async def get_environment(db: AsyncSession, env_id: int) -> db_models.Environment | None:
    """通过 ID 获取环境"""
    result = await db.execute(
        select(db_models.Environment).where(db_models.Environment.id == env_id)
    )
    return result.scalar_one_or_none()


async def create_environment(db: AsyncSession, project_id: int, env: models.EnvironmentCreate) -> db_models.Environment:
    """创建环境，若为第一个环境则自动设为默认"""
    count_result = await db.execute(
        select(db_models.Environment).where(
            db_models.Environment.project_id == project_id
        )
    )
    existing = len(count_result.scalars().all())

    db_env = db_models.Environment(
        project_id=project_id,
        name=env.name,
        base_url=env.base_url,
        browser=env.browser,
        headless=env.headless,
        cookies=env.cookies or [],
        is_default=(existing == 0),
    )
    db.add(db_env)
    try:
        await db.commit()
        await db.refresh(db_env)
    except Exception as e:
        await db.rollback()
        raise ValueError(f"创建环境失败: {e}") from e

    # 如果是默认环境，同步到 Project
    if db_env.is_default:
        await _sync_env_to_project(db, project_id, db_env)

    return db_env


async def update_environment(db: AsyncSession, env_id: int, env: models.EnvironmentUpdate) -> db_models.Environment | None:
    """更新环境"""
    db_env = await get_environment(db, env_id)
    if not db_env:
        return None

    update_data = env.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(db_env, key, value)

    try:
        await db.commit()
        await db.refresh(db_env)
    except Exception as e:
        await db.rollback()
        raise ValueError(f"更新环境失败: {e}") from e

    # 如果是默认环境，同步到 Project
    if db_env.is_default:
        await _sync_env_to_project(db, db_env.project_id, db_env)

    return db_env


async def delete_environment(db: AsyncSession, env_id: int) -> dict[str, str] | None:
    """删除环境"""
    db_env = await get_environment(db, env_id)
    if not db_env:
        return None

    project_id = db_env.project_id

    await db.delete(db_env)
    try:
        await db.commit()
    except Exception as e:
        await db.rollback()
        raise ValueError(f"删除环境失败: {e}") from e

    # 如果删除了默认环境，指定另一个环境为默认
    remaining_result = await db.execute(
        select(db_models.Environment)
        .where(db_models.Environment.project_id == project_id)
        .order_by(db_models.Environment.created_at.asc())
    )
    remaining = remaining_result.scalars().first()
    if remaining:
        remaining.is_default = True
        try:
            await db.commit()
            await db.refresh(remaining)
        except Exception as e:
            await db.rollback()
            raise ValueError(f"设置新默认环境失败: {e}") from e
        await _sync_env_to_project(db, project_id, remaining)

    return {"message": f"环境 {env_id} 已删除"}


async def set_default_environment(db: AsyncSession, env_id: int) -> db_models.Environment | None:
    """设为默认环境，同时同步到 Project"""
    db_env = await get_environment(db, env_id)
    if not db_env:
        return None

    # 清除该项目的所有默认标记
    await db.execute(
        update(db_models.Environment)
        .where(db_models.Environment.project_id == db_env.project_id)
        .values({db_models.Environment.is_default: False})
    )

    db_env.is_default = True
    try:
        await db.commit()
        await db.refresh(db_env)
    except Exception as e:
        await db.rollback()
        raise ValueError(f"设置默认环境失败: {e}") from e

    # 同步到 Project
    await _sync_env_to_project(db, db_env.project_id, db_env)

    return db_env


async def ensure_default_environment(db: AsyncSession, project_id: int) -> None:
    """为有 base_url 的旧项目自动创建默认环境"""
    count_result = await db.execute(
        select(db_models.Environment).where(
            db_models.Environment.project_id == project_id
        )
    )
    existing = len(count_result.scalars().all())
    if existing > 0:
        return

    project = await get_project(db, project_id)
    if not project or not project.base_url:
        return

    env = db_models.Environment(
        project_id=project_id,
        name="default",
        base_url=project.base_url,
        browser=project.browser or "chromium",
        headless=project.headless if project.headless is not None else True,
        is_default=True,
    )
    db.add(env)
    await db.commit()


async def _sync_env_to_project(db: AsyncSession, project_id: int, env) -> None:
    """将环境配置同步回 Project 字段"""
    project = await get_project(db, project_id)
    if not project:
        return
    project.base_url = env.base_url
    project.browser = env.browser
    project.headless = env.headless
    await db.commit()
