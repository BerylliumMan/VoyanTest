# app/crud/environment.py - 环境 CRUD
import logging

from sqlalchemy.orm import Session

from app import db_models, models
from app.crud.project import get_project

logger = logging.getLogger(__name__)


# ----------------------------
# 环境 CRUD
# ----------------------------

def get_environments(db: Session, project_id: int) -> list[db_models.Environment]:
    """获取项目的所有环境"""
    return db.query(db_models.Environment).filter(
        db_models.Environment.project_id == project_id
    ).order_by(db_models.Environment.created_at.asc()).all()


def get_environment(db: Session, env_id: int) -> db_models.Environment | None:
    """通过 ID 获取环境"""
    return db.query(db_models.Environment).filter(db_models.Environment.id == env_id).first()


def create_environment(db: Session, project_id: int, env: models.EnvironmentCreate) -> db_models.Environment:
    """创建环境，若为第一个环境则自动设为默认"""
    existing = db.query(db_models.Environment).filter(
        db_models.Environment.project_id == project_id
    ).count()

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
    db.commit()
    db.refresh(db_env)

    # 如果是默认环境，同步到 Project
    if db_env.is_default:
        _sync_env_to_project(db, project_id, db_env)

    return db_env


def update_environment(db: Session, env_id: int, env: models.EnvironmentUpdate) -> db_models.Environment | None:
    """更新环境"""
    db_env = get_environment(db, env_id)
    if not db_env:
        return None

    update_data = env.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(db_env, key, value)

    db.commit()
    db.refresh(db_env)

    # 如果是默认环境，同步到 Project
    if db_env.is_default:
        _sync_env_to_project(db, db_env.project_id, db_env)

    return db_env


def delete_environment(db: Session, env_id: int) -> dict[str, str] | None:
    """删除环境"""
    db_env = get_environment(db, env_id)
    if not db_env:
        return None

    project_id = db_env.project_id

    db.delete(db_env)
    db.commit()

    # 如果删除了默认环境，指定另一个环境为默认
    remaining = db.query(db_models.Environment).filter(
        db_models.Environment.project_id == project_id
    ).order_by(db_models.Environment.created_at.asc()).first()
    if remaining:
        remaining.is_default = True
        db.commit()
        db.refresh(remaining)
        _sync_env_to_project(db, project_id, remaining)

    return {"message": f"环境 {env_id} 已删除"}


def set_default_environment(db: Session, env_id: int) -> db_models.Environment | None:
    """设为默认环境，同时同步到 Project"""
    db_env = get_environment(db, env_id)
    if not db_env:
        return None

    # 清除该项目的所有默认标记
    db.query(db_models.Environment).filter(
        db_models.Environment.project_id == db_env.project_id
    ).update({db_models.Environment.is_default: False})

    db_env.is_default = True
    db.commit()
    db.refresh(db_env)

    # 同步到 Project
    _sync_env_to_project(db, db_env.project_id, db_env)

    return db_env


def ensure_default_environment(db: Session, project_id: int) -> None:
    """为有 base_url 的旧项目自动创建默认环境"""
    existing = db.query(db_models.Environment).filter(
        db_models.Environment.project_id == project_id
    ).count()
    if existing > 0:
        return

    project = get_project(db, project_id)
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
    db.commit()


def _sync_env_to_project(db: Session, project_id: int, env) -> None:
    """将环境配置同步回 Project 字段"""
    project = get_project(db, project_id)
    if not project:
        return
    project.base_url = env.base_url
    project.browser = env.browser
    project.headless = env.headless
    db.commit()