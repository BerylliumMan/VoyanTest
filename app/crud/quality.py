# 042 质量决策与治理：数据访问层
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import db_models


async def list_regression_plans(db: AsyncSession, project_id: int) -> list[db_models.QualityRegressionPlan]:
    rows = await db.execute(
        select(db_models.QualityRegressionPlan)
        .where(db_models.QualityRegressionPlan.project_id == project_id)
        .order_by(db_models.QualityRegressionPlan.created_at.desc())
    )
    return list(rows.scalars().all())


async def get_regression_plan(db: AsyncSession, plan_id: int) -> db_models.QualityRegressionPlan | None:
    return await db.get(db_models.QualityRegressionPlan, plan_id)


async def create_regression_plan(db: AsyncSession, **kwargs) -> db_models.QualityRegressionPlan:
    row = db_models.QualityRegressionPlan(**kwargs)
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def update_regression_plan(db: AsyncSession, row: db_models.QualityRegressionPlan, **kwargs):
    for key, value in kwargs.items():
        if value is not None:
            setattr(row, key, value)
    await db.commit()
    await db.refresh(row)
    return row


async def delete_regression_plan(db: AsyncSession, row: db_models.QualityRegressionPlan) -> None:
    await db.delete(row)
    await db.commit()


async def get_flaky(db: AsyncSession, case_id: int) -> db_models.QualityFlakyState | None:
    result = await db.execute(
        select(db_models.QualityFlakyState).where(db_models.QualityFlakyState.case_id == case_id)
    )
    return result.scalar_one_or_none()


async def list_flaky(db: AsyncSession, project_id: int, status: str | None = None) -> list[db_models.QualityFlakyState]:
    stmt = select(db_models.QualityFlakyState).where(db_models.QualityFlakyState.project_id == project_id)
    if status:
        stmt = stmt.where(db_models.QualityFlakyState.status == status)
    rows = await db.execute(stmt.order_by(db_models.QualityFlakyState.updated_at.desc()))
    return list(rows.scalars().all())


async def upsert_flaky(db: AsyncSession, *, project_id: int, case_id: int, **kwargs) -> db_models.QualityFlakyState:
    row = await get_flaky(db, case_id)
    if row is None:
        row = db_models.QualityFlakyState(project_id=project_id, case_id=case_id, **kwargs)
        db.add(row)
    else:
        if row.project_id != project_id:
            raise ValueError("用例不属于当前项目")
        for key, value in kwargs.items():
            setattr(row, key, value)
    await db.commit()
    await db.refresh(row)
    return row


async def quarantined_case_ids(db: AsyncSession, case_ids: list[int]) -> set[int]:
    if not case_ids:
        return set()
    rows = await db.execute(
        select(db_models.QualityFlakyState.case_id).where(
            db_models.QualityFlakyState.case_id.in_(case_ids),
            db_models.QualityFlakyState.status == "quarantined",
        )
    )
    return {int(x) for x in rows.scalars().all()}


async def list_requirements(db: AsyncSession, project_id: int, include_archived: bool = False):
    stmt = select(db_models.QualityRequirement).where(db_models.QualityRequirement.project_id == project_id)
    if not include_archived:
        stmt = stmt.where(db_models.QualityRequirement.status == "active")
    rows = await db.execute(stmt.order_by(db_models.QualityRequirement.name))
    return list(rows.scalars().all())


async def get_requirement(db: AsyncSession, requirement_id: int) -> db_models.QualityRequirement | None:
    return await db.get(db_models.QualityRequirement, requirement_id)


async def create_requirement(db: AsyncSession, **kwargs) -> db_models.QualityRequirement:
    row = db_models.QualityRequirement(**kwargs)
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def update_requirement(db: AsyncSession, row: db_models.QualityRequirement, **kwargs):
    for key, value in kwargs.items():
        if value is not None:
            setattr(row, key, value)
    await db.commit()
    await db.refresh(row)
    return row


async def delete_requirement(db: AsyncSession, row: db_models.QualityRequirement) -> None:
    await db.delete(row)
    await db.commit()


async def list_links(db: AsyncSession, requirement_id: int) -> list[db_models.QualityRequirementLink]:
    rows = await db.execute(
        select(db_models.QualityRequirementLink)
        .where(db_models.QualityRequirementLink.requirement_id == requirement_id)
    )
    return list(rows.scalars().all())


async def link_case(db: AsyncSession, *, requirement_id: int, case_id: int, case_kind: str,
                     source: str = "manual", created_by: int | None = None) -> db_models.QualityRequirementLink:
    existing = await db.execute(
        select(db_models.QualityRequirementLink).where(
            db_models.QualityRequirementLink.requirement_id == requirement_id,
            db_models.QualityRequirementLink.case_id == case_id,
        )
    )
    row = existing.scalar_one_or_none()
    if row:
        return row
    row = db_models.QualityRequirementLink(
        requirement_id=requirement_id, case_id=case_id, case_kind=case_kind,
        source=source, created_by=created_by,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def unlink_case(db: AsyncSession, requirement_id: int, case_id: int) -> bool:
    res = await db.execute(
        delete(db_models.QualityRequirementLink).where(
            db_models.QualityRequirementLink.requirement_id == requirement_id,
            db_models.QualityRequirementLink.case_id == case_id,
        )
    )
    await db.commit()
    return bool(res.rowcount)


async def unlinked_case_ids(db: AsyncSession, project_id: int) -> set[int]:
    linked = await db.execute(
        select(db_models.QualityRequirementLink.case_id)
        .join(db_models.QualityRequirement, db_models.QualityRequirement.id == db_models.QualityRequirementLink.requirement_id)
        .where(db_models.QualityRequirement.project_id == project_id)
    )
    return {int(x) for x in linked.scalars().all()}
