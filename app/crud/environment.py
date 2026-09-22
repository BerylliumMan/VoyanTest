# app/crud/environment.py - 环境 CRUD
import logging
from typing import Any

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
        variables=env.variables or [],
        headers=env.headers or [],
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
    if "variables" in update_data:
        update_data["variables"] = _merge_masked_variables(
            db_env.variables or [], update_data["variables"] or []
        )
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


def _merge_masked_variables(old_items: list, new_items: list) -> list:
    """写入幂等（契约 §1.6）：secret 项回传 value == "******" 表示未修改，
    保留库中原值；非 secret 项原样落库。"""
    old_secret_values: dict[str, Any] = {}
    for it in old_items or []:
        if isinstance(it, dict) and it.get("secret"):
            key = str(it.get("key") or "").strip()
            if key:
                old_secret_values[key] = it.get("value")
    out = []
    for it in new_items or []:
        if not isinstance(it, dict):
            out.append(it)
            continue
        item = dict(it)
        key = str(item.get("key") or "").strip()
        if item.get("secret") and item.get("value") == "******" and key in old_secret_values:
            item["value"] = old_secret_values[key]
        out.append(item)
    return out


async def update_environment_variables(
    db: AsyncSession, env_id: int, updates: dict[str, str]
) -> bool:
    """把 environment 作用域提取的变量写回 environments.variables。

    保留原项的 secret/enable 元数据，只更新 value；新 key 追加
    ``{"key","value","secret":False,"enable":True}``。
    注意：必须重建 dict 对象（JSON 列赋值时新旧值 == 不会触发 UPDATE）。
    """
    db_env = await get_environment(db, env_id)
    if db_env is None:
        return False
    old_items = db_env.variables or []
    by_key: dict[str, dict] = {}
    for it in old_items:
        if isinstance(it, dict) and str(it.get("key") or "").strip():
            by_key[str(it["key"]).strip()] = it
    new_items: list[dict] = []
    updated_keys: set[str] = set()
    for it in old_items:
        if not isinstance(it, dict):
            new_items.append(it)
            continue
        key = str(it.get("key") or "").strip()
        if key in updates:
            new_items.append({**it, "value": updates[key]})
            updated_keys.add(key)
        else:
            new_items.append(dict(it))
    for key, value in updates.items():
        if key not in updated_keys:
            new_items.append({"key": key, "value": value, "secret": False, "enable": True})
    db_env.variables = new_items
    try:
        await db.commit()
        await db.refresh(db_env)
    except Exception as e:
        await db.rollback()
        raise ValueError(f"更新环境变量失败: {e}") from e
    return True


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
