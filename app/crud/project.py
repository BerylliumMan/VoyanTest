# app/crud/project.py - 项目 CRUD
import logging
from collections.abc import Generator

from sqlalchemy.orm import Session

from app.database import SessionLocal
from app import db_models, models

logger = logging.getLogger(__name__)


# 获取数据库会话
def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ----------------------------
# 项目CRUD
# ----------------------------

def create_project(db: Session, project: models.ProjectCreate) -> db_models.Project:
    """创建新项目"""
    db_project = db_models.Project(
        name=project.name,
        description=project.description,
        base_url=project.base_url,
        browser=project.browser,
        headless=project.headless
    )
    db.add(db_project)
    db.commit()
    db.refresh(db_project)
    return db_project

def get_project(db: Session, project_id: int) -> db_models.Project | None:
    """通过ID获取项目"""
    return db.query(db_models.Project).filter(db_models.Project.id == project_id).first()

def get_all_projects(db: Session) -> list[db_models.Project]:
    """获取所有项目"""
    return db.query(db_models.Project).order_by(db_models.Project.created_at.desc()).all()

def update_project(db: Session, project_id: int, project: models.ProjectUpdate) -> db_models.Project | None:
    """更新项目"""
    db_project = get_project(db, project_id)
    if not db_project:
        return None

    update_data = project.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(db_project, key, value)

    db.commit()
    db.refresh(db_project)
    return db_project

def delete_project(db: Session, project_id: int) -> dict[str, str] | None:
    """删除项目及其关联数据"""
    db_project = get_project(db, project_id)
    if not db_project:
        return None

    # 按顺序手动删除关联数据，避免 FK 约束冲突
    # 1. 删除所有测试用例的步骤
    from app.db_models import TestStep, TestCase, Module
    db.query(TestStep).filter(
        TestStep.case_id.in_(
            db.query(TestCase.id).filter(TestCase.project_id == project_id)
        )
    ).delete(synchronize_session=False)

    # 2. 删除所有测试用例
    db.query(TestCase).filter(TestCase.project_id == project_id).delete(synchronize_session=False)

    # 3. 删除所有模块
    db.query(Module).filter(Module.project_id == project_id).delete(synchronize_session=False)

    # 4. 删除所有环境
    db.query(db_models.Environment).filter(
        db_models.Environment.project_id == project_id
    ).delete(synchronize_session=False)

    # 5. 删除项目本身
    db.delete(db_project)
    db.commit()
    return {"message": f"项目 {project_id} 及其所有资产已删除。"}