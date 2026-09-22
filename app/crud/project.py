# app/crud/project.py - 项目 CRUD
import logging

from sqlalchemy import delete, or_, select, update
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
    """删除项目及其关联资产（用例 / 运行 / 接口测试 / 模块 / 环境）。

    各表 FK 多数没有 ON DELETE CASCADE，必须按依赖从叶子删到根，
    否则会撞 ``test_runs.case_id`` / ``api_definitions.project_id`` 等约束变成 500。
    """
    db_project = await get_project(db, project_id)
    if not db_project:
        return None

    from app.db_models import (
        ApiDataset,
        ApiDefinition,
        ApiImport,
        ApiScenario,
        GenFunctionalPoint,
        GenSession,
        GenTestCase,
        Module,
        RunBatch,
        RunLog,
        ScheduledTaskRun,
        TestCase,
        TestRun,
        TestStep,
        TestSuite,
        TestSuiteCase,
    )

    case_id_list = list(
        (await db.execute(select(TestCase.id).where(TestCase.project_id == project_id))).scalars()
    )
    batch_id_list = list(
        (await db.execute(select(RunBatch.id).where(RunBatch.project_id == project_id))).scalars()
    )
    run_filters = []
    if case_id_list:
        run_filters.append(TestRun.case_id.in_(case_id_list))
    if batch_id_list:
        run_filters.append(TestRun.batch_id.in_(batch_id_list))
    run_id_list: list[int] = []
    if run_filters:
        run_id_list = list(
            (await db.execute(select(TestRun.id).where(or_(*run_filters)))).scalars()
        )
    session_id_list = list(
        (await db.execute(select(GenSession.id).where(GenSession.project_id == project_id))).scalars()
    )

    if run_id_list:
        await db.execute(delete(ScheduledTaskRun).where(ScheduledTaskRun.run_id.in_(run_id_list)))
        await db.execute(delete(RunLog).where(RunLog.run_id.in_(run_id_list)))
        await db.execute(delete(TestRun).where(TestRun.id.in_(run_id_list)))
    if batch_id_list:
        await db.execute(delete(RunBatch).where(RunBatch.id.in_(batch_id_list)))

    if case_id_list:
        await db.execute(delete(TestStep).where(TestStep.case_id.in_(case_id_list)))
        await db.execute(delete(TestSuiteCase).where(TestSuiteCase.case_id.in_(case_id_list)))
    await db.execute(delete(TestSuite).where(TestSuite.project_id == project_id))

    await db.execute(delete(ApiScenario).where(ApiScenario.project_id == project_id))
    await db.execute(delete(ApiDefinition).where(ApiDefinition.project_id == project_id))
    await db.execute(delete(ApiImport).where(ApiImport.project_id == project_id))
    await db.execute(delete(ApiDataset).where(ApiDataset.project_id == project_id))

    if session_id_list:
        await db.execute(delete(GenTestCase).where(GenTestCase.session_id.in_(session_id_list)))
        await db.execute(
            delete(GenFunctionalPoint).where(GenFunctionalPoint.session_id.in_(session_id_list))
        )
    await db.execute(delete(GenSession).where(GenSession.project_id == project_id))

    await db.execute(
        update(TestCase).where(TestCase.project_id == project_id).values(parent_id=None)
    )
    await db.execute(delete(TestCase).where(TestCase.project_id == project_id))

    await db.execute(
        update(Module).where(Module.project_id == project_id).values(parent_id=None)
    )
    await db.execute(delete(Module).where(Module.project_id == project_id))

    await db.execute(
        delete(db_models.Environment).where(db_models.Environment.project_id == project_id)
    )

    await db.delete(db_project)
    try:
        await db.commit()
    except Exception as e:
        await db.rollback()
        raise ValueError(f"删除项目失败: {e}") from e
    return {"message": f"项目 {project_id} 及其所有资产已删除。"}
