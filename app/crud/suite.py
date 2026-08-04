# app/crud/suite.py — 测试用例集 CRUD
from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app import db_models
from app.tz import now as tz_now


async def _validate_case_ids(
    db: AsyncSession,
    project_id: int,
    case_ids: list[int],
    case_kind: str | None = None,
) -> list[db_models.TestCase]:
    """校验用例存在且同属项目；保持传入顺序返回。"""
    if not case_ids:
        return []
    result = await db.execute(
        select(db_models.TestCase).where(db_models.TestCase.id.in_(case_ids))
    )
    by_id = {tc.id: tc for tc in result.scalars().all()}
    ordered: list[db_models.TestCase] = []
    for cid in case_ids:
        tc = by_id.get(cid)
        if tc is None:
            raise ValueError(f"Test case {cid} not found")
        if tc.project_id != project_id:
            raise ValueError(f"Test case {cid} is not in project {project_id}")
        if case_kind and getattr(tc, "case_kind", None) and tc.case_kind != case_kind:
            raise ValueError(f"Test case {cid} case_kind mismatch (expected {case_kind})")
        ordered.append(tc)
    return ordered


def suite_to_dict(suite: db_models.TestSuite) -> dict:
    cases = sorted(suite.cases or [], key=lambda c: c.order_index)
    items = []
    for sc in cases:
        name = None
        module_id = None
        # relationship may not load TestCase; leave name None if unavailable
        items.append({
            "case_id": sc.case_id,
            "order_index": sc.order_index,
            "name": name,
            "module_id": module_id,
        })
    return {
        "id": suite.id,
        "project_id": suite.project_id,
        "name": suite.name,
        "description": suite.description,
        "case_kind": suite.case_kind,
        "case_count": len(cases),
        "cases": items,
        "created_at": suite.created_at,
        "updated_at": suite.updated_at,
    }


async def enrich_suite_cases(db: AsyncSession, payload: dict) -> dict:
    """为 suite dict 填充用例 name / module_id。"""
    case_ids = [c["case_id"] for c in payload.get("cases") or []]
    if not case_ids:
        return payload
    result = await db.execute(
        select(db_models.TestCase).where(db_models.TestCase.id.in_(case_ids))
    )
    by_id = {tc.id: tc for tc in result.scalars().all()}
    for item in payload["cases"]:
        tc = by_id.get(item["case_id"])
        if tc:
            item["name"] = tc.name
            item["module_id"] = tc.module_id
    return payload


async def list_suites(
    db: AsyncSession,
    project_id: int,
    case_kind: str | None = None,
) -> list[db_models.TestSuite]:
    q = (
        select(db_models.TestSuite)
        .options(selectinload(db_models.TestSuite.cases))
        .where(db_models.TestSuite.project_id == project_id)
        .order_by(db_models.TestSuite.id.desc())
    )
    if case_kind:
        q = q.where(db_models.TestSuite.case_kind == case_kind)
    result = await db.execute(q)
    return list(result.scalars().all())


async def get_suite(db: AsyncSession, suite_id: int) -> db_models.TestSuite | None:
    result = await db.execute(
        select(db_models.TestSuite)
        .options(selectinload(db_models.TestSuite.cases))
        .where(db_models.TestSuite.id == suite_id)
    )
    return result.scalar_one_or_none()


async def get_suite_ordered_case_ids(db: AsyncSession, suite_id: int) -> list[int]:
    result = await db.execute(
        select(db_models.TestSuiteCase.case_id)
        .where(db_models.TestSuiteCase.suite_id == suite_id)
        .order_by(db_models.TestSuiteCase.order_index.asc(), db_models.TestSuiteCase.id.asc())
    )
    return list(result.scalars().all())


def _dedupe_preserve_order(case_ids: list[int]) -> list[int]:
    seen: set[int] = set()
    out: list[int] = []
    for cid in case_ids:
        if cid in seen:
            continue
        seen.add(cid)
        out.append(cid)
    return out


async def create_suite(
    db: AsyncSession,
    project_id: int,
    name: str,
    case_ids: list[int],
    *,
    description: str | None = None,
    case_kind: str = "ui",
) -> db_models.TestSuite:
    case_ids = _dedupe_preserve_order(list(case_ids or []))
    await _validate_case_ids(db, project_id, case_ids, case_kind=case_kind)
    suite = db_models.TestSuite(
        project_id=project_id,
        name=name,
        description=description,
        case_kind=case_kind,
    )
    db.add(suite)
    await db.flush()
    for idx, cid in enumerate(case_ids):
        db.add(db_models.TestSuiteCase(suite_id=suite.id, case_id=cid, order_index=idx))
    await db.commit()
    return await get_suite(db, suite.id)


async def update_suite(
    db: AsyncSession,
    suite_id: int,
    *,
    name: str | None = None,
    description: str | None = None,
    case_ids: list[int] | None = None,
) -> db_models.TestSuite | None:
    suite = await get_suite(db, suite_id)
    if suite is None:
        return None
    if name is not None:
        suite.name = name
    if description is not None:
        suite.description = description
    suite.updated_at = tz_now()
    if case_ids is not None:
        case_ids = _dedupe_preserve_order(list(case_ids))
        await _validate_case_ids(db, suite.project_id, case_ids, case_kind=suite.case_kind)
        await db.execute(
            delete(db_models.TestSuiteCase).where(db_models.TestSuiteCase.suite_id == suite_id)
        )
        for idx, cid in enumerate(case_ids):
            db.add(db_models.TestSuiteCase(suite_id=suite_id, case_id=cid, order_index=idx))
    await db.commit()
    return await get_suite(db, suite_id)


async def delete_suite(db: AsyncSession, suite_id: int) -> bool:
    suite = await get_suite(db, suite_id)
    if suite is None:
        return False
    await db.delete(suite)
    await db.commit()
    return True
