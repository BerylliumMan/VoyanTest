# app/crud/project.py - 项目 CRUD
import logging

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app import db_models, models

logger = logging.getLogger(__name__)


# ----------------------------
# 项目CRUD
# ----------------------------

async def create_project(db: AsyncSession, project: models.ProjectCreate) -> db_models.Project:
    db_project = db_models.Project(
        name=project.name,
        description=project.description,
        base_url=project.base_url,
        browser=project.browser,
        headless=project.headless
    )
    db.add(db_project)
    try:
        await db.commit()
        await db.refresh(db_project)
    except Exception as e:
        await db.rollback()
        raise ValueError(f"创建项目失败: {e}") from e
    return db_project

async def get_project(db: AsyncSession, project_id: int) -> db_models.Project | None:
    result = await db.execute(
        select(db_models.Project).where(db_models.Project.id == project_id)
    )
    return result.scalar_one_or_none()

async def get_all_projects(db: AsyncSession) -> list[db_models.Project]:
    result = await db.execute(
        select(db_models.Project).order_by(db_models.Project.created_at.desc())
    )
    return result.scalars().all()


async def list_projects_for_user(db: AsyncSession, allowed_ids: list[int] | None) -> list[db_models.Project]:
    stmt = select(db_models.Project)
    if allowed_ids is not None:
        stmt = stmt.where(db_models.Project.id.in_(allowed_ids))
    stmt = stmt.order_by(db_models.Project.created_at.desc())
    result = await db.execute(stmt)
    return result.scalars().all()

async def update_project(db: AsyncSession, project_id: int, project: models.ProjectUpdate) -> db_models.Project | None:
    db_project = await get_project(db, project_id)
    if not db_project:
        return None

    update_data = project.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(db_project, key, value)

    try:
        await db.commit()
        await db.refresh(db_project)
    except Exception as e:
        await db.rollback()
        raise ValueError(f"更新项目失败: {e}") from e
    return db_project

async def delete_project(db: AsyncSession, project_id: int) -> dict[str, str] | None:
    db_project = await get_project(db, project_id)
    if not db_project:
        return None

    from app.db_models import TestStep, TestCase, Module
    await db.execute(
        delete(TestStep).where(
            TestStep.case_id.in_(
                select(TestCase.id).where(TestCase.project_id == project_id)
            )
        )
    )

    await db.execute(delete(TestCase).where(TestCase.project_id == project_id))

    await db.execute(delete(Module).where(Module.project_id == project_id))

    await db.execute(
        delete(db_models.Environment).where(
            db_models.Environment.project_id == project_id
        )
    )

    await db.delete(db_project)
    try:
        await db.commit()
    except Exception as e:
        await db.rollback()
        raise ValueError(f"删除项目失败: {e}") from e
    return {"message": f"项目 {project_id} 及其所有资产已删除。"}
