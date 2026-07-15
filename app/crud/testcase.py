# app/crud/testcase.py - 测试步骤 + 测试用例 CRUD
import logging

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app import db_models, models

logger = logging.getLogger(__name__)


# ----------------------------
# 测试步骤CRUD
# ----------------------------

async def create_test_step(db: AsyncSession, step: models.TestStepCreate) -> db_models.TestStep:
    """创建测试步骤（不 commit，由调用者负责）"""
    db_step = db_models.TestStep(
        case_id=step.case_id,
        step_order=step.step_order,
        description=step.description,
        parsed_result=step.parsed_result
    )
    db.add(db_step)
    await db.flush()
    await db.refresh(db_step)
    return db_step

async def get_steps_for_case(db: AsyncSession, case_id: int) -> list[db_models.TestStep]:
    """获取测试用例的所有步骤"""
    result = await db.execute(
        select(db_models.TestStep)
        .where(db_models.TestStep.case_id == case_id)
        .order_by(db_models.TestStep.step_order.asc())
    )
    return result.scalars().all()

async def delete_steps_for_case(db: AsyncSession, case_id: int) -> None:
    """删除测试用例的所有步骤（不 commit，由调用者负责）"""
    # 先将 RunLog 中对这些步骤的引用置空，否则 FK 约束会阻止删除
    subq = select(db_models.TestStep.id).where(db_models.TestStep.case_id == case_id)
    await db.execute(
        update(db_models.RunLog)
        .where(db_models.RunLog.step_id.in_(subq))
        .values(step_id=None)
    )
    await db.execute(delete(db_models.TestStep).where(db_models.TestStep.case_id == case_id))

# ----------------------------
# 测试用例CRUD
# ----------------------------

async def get_next_project_case_number(db: AsyncSession, project_id: int) -> int:
    """获取项目内下一个用例编号"""
    result = await db.execute(
        select(db_models.TestCase.project_case_number)
        .where(db_models.TestCase.project_id == project_id)
        .order_by(db_models.TestCase.project_case_number.desc())
    )
    max_num = result.scalar()
    return (max_num + 1) if max_num else 1


async def create_test_case(db: AsyncSession, case: models.TestCaseCreate) -> db_models.TestCase:
    """创建测试用例及其步骤（单事务）"""
    db_case = db_models.TestCase(
        project_id=case.project_id,
        module_id=case.module_id,
        project_case_number=await get_next_project_case_number(db, case.project_id),
        name=case.name,
        description=case.description
    )
    db.add(db_case)
    await db.flush()

    # 创建步骤
    created_steps = []
    for step_data in case.steps:
        db_step = db_models.TestStep(
            case_id=db_case.id,
            step_order=step_data.step_order,
            description=step_data.description,
            parsed_result=step_data.parsed_result
        )
        db.add(db_step)
        created_steps.append(db_step)

    await db.commit()
    await db.refresh(db_case)
    db_case.steps = created_steps
    return db_case

async def get_test_case(db: AsyncSession, case_id: int) -> db_models.TestCase | None:
    """通过ID获取测试用例"""
    result = await db.execute(
        select(db_models.TestCase)
        .options(selectinload(db_models.TestCase.steps))
        .where(db_models.TestCase.id == case_id)
    )
    return result.scalar_one_or_none()

async def get_all_test_cases_for_project(db: AsyncSession, project_id: int) -> list[db_models.TestCase]:
    """获取项目的所有测试用例"""
    result = await db.execute(
        select(db_models.TestCase)
        .options(selectinload(db_models.TestCase.steps), selectinload(db_models.TestCase.module))
        .where(db_models.TestCase.project_id == project_id)
        .order_by(db_models.TestCase.project_case_number.asc())
    )
    return result.scalars().all()

async def get_all_test_cases_for_project_paginated(db: AsyncSession, project_id: int, page: int = 1, size: int = 20) -> dict[str, any]:
    """分页获取项目的测试用例"""
    count_result = await db.execute(
        select(func.count(db_models.TestCase.id))
        .where(db_models.TestCase.project_id == project_id)
    )
    total_items = count_result.scalar()
    offset = (page - 1) * size
    items_result = await db.execute(
        select(db_models.TestCase)
        .options(selectinload(db_models.TestCase.steps))
        .where(db_models.TestCase.project_id == project_id)
        .order_by(db_models.TestCase.id.asc())
        .offset(offset)
        .limit(size)
    )
    items = items_result.scalars().all()
    return {"total_items": total_items, "items": items}

async def get_all_test_cases_for_module(db: AsyncSession, module_id: int) -> list[db_models.TestCase]:
    """获取模块的所有测试用例"""
    result = await db.execute(
        select(db_models.TestCase)
        .options(selectinload(db_models.TestCase.steps))
        .where(db_models.TestCase.module_id == module_id)
        .order_by(db_models.TestCase.id.asc())
    )
    return result.scalars().all()

async def get_all_test_cases_for_module_paginated(db: AsyncSession, module_id: int, page: int = 1, size: int = 20) -> dict[str, any]:
    """获取模块的所有测试用例（分页）"""
    count_result = await db.execute(
        select(func.count(db_models.TestCase.id))
        .where(db_models.TestCase.module_id == module_id)
    )
    total_items = count_result.scalar()
    offset = (page - 1) * size
    items_result = await db.execute(
        select(db_models.TestCase)
        .options(selectinload(db_models.TestCase.steps))
        .where(db_models.TestCase.module_id == module_id)
        .order_by(db_models.TestCase.id.asc())
        .offset(offset)
        .limit(size)
    )
    items = items_result.scalars().all()
    return {"total_items": total_items, "items": items}

async def search_test_cases(db: AsyncSession, project_id: int, query: str, page: int = 1, size: int = 20) -> dict[str, any]:
    """根据名称和描述搜索测试用例"""
    conditions = [db_models.TestCase.project_id == project_id]
    if query:
        like = f"%{query}%"
        conditions.append(
            (db_models.TestCase.name.ilike(like)) | (db_models.TestCase.description.ilike(like))
        )

    count_result = await db.execute(
        select(func.count(db_models.TestCase.id)).where(*conditions)
    )
    total = count_result.scalar()

    items_result = await db.execute(
        select(db_models.TestCase)
        .options(selectinload(db_models.TestCase.steps))
        .where(*conditions)
        .order_by(db_models.TestCase.id.asc())
        .offset((page - 1) * size)
        .limit(size)
    )
    items = items_result.scalars().all()
    return {"total_items": total, "items": items}

async def update_test_case(db: AsyncSession, case_id: int, case: models.TestCaseUpdate) -> db_models.TestCase | None:
    """更新测试用例（单事务）"""
    db_case = await get_test_case(db, case_id)
    if not db_case:
        return None

    # 更新基本信息
    db_case.name = case.name
    db_case.description = case.description
    db_case.module_id = case.module_id

    # 删除旧步骤并创建新步骤（单事务）
    await delete_steps_for_case(db, case_id)

    for step_data in case.steps:
        db_step = db_models.TestStep(
            case_id=case_id,
            step_order=step_data.step_order,
            description=step_data.description,
            parsed_result=step_data.parsed_result
        )
        db.add(db_step)

    await db.commit()
    await db.refresh(db_case)
    return db_case

async def update_test_case_is_init(db: AsyncSession, case_id: int, is_init: bool) -> db_models.TestCase | None:
    """切换测试用例的初始化标记"""
    db_case = await get_test_case(db, case_id)
    if not db_case:
        return None
    db_case.is_init = is_init
    await db.commit()
    await db.refresh(db_case)
    return db_case


async def get_init_test_cases(db: AsyncSession, project_id: int) -> list[db_models.TestCase]:
    """获取项目下所有标记为初始化的测试用例"""
    result = await db.execute(
        select(db_models.TestCase)
        .options(selectinload(db_models.TestCase.steps))
        .where(
            db_models.TestCase.project_id == project_id,
            db_models.TestCase.is_init == True,
        )
        .order_by(db_models.TestCase.created_at.desc())
    )
    return result.scalars().all()


async def delete_test_case(db: AsyncSession, case_id: int) -> dict[str, str] | None:
    """删除测试用例（步骤一起删，运行记录保留但解除关联）"""
    db_case = await get_test_case(db, case_id)
    if not db_case:
        return None

    # 删除关联的步骤
    await delete_steps_for_case(db, case_id)

    # 解除运行记录的外键关联（保留报告记录）
    await db.execute(text("UPDATE test_runs SET case_id = NULL WHERE case_id = :cid"), {"cid": case_id})

    await db.delete(db_case)
    await db.commit()
    return {"message": f"测试用例 {case_id} 及其步骤已删除。"}
