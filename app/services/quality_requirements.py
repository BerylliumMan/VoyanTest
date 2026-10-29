# 042 US3：需求条目 / 关联 / 覆盖矩阵
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import crud, db_models
from app.crud import quality as cq


async def import_from_session(db: AsyncSession, *, project_id: int, session_id: str,
                              module_id: int | None, user_id: int | None) -> dict:
    from app.crud import gen as crud_gen
    fps = await crud_gen.list_gen_functional_points(db, session_id)
    created = 0
    skipped = 0
    items = []
    existing = {r.name for r in await cq.list_requirements(db, project_id, include_archived=True)}
    for fp in fps:
        name = (fp.name or "").strip()
        if not name or name in existing:
            skipped += 1
            continue
        row = await cq.create_requirement(
            db, project_id=project_id, name=name[:300],
            module_id=module_id, source="generation", source_session_id=session_id,
            description=fp.description, created_by=user_id,
        )
        existing.add(name)
        created += 1
        items.append({"id": row.id, "name": row.name})
    return {"created": created, "skipped": skipped, "items": items}


async def matrix(db: AsyncSession, *, project_id: int, case_kind: str | None = None) -> dict:
    reqs = await cq.list_requirements(db, project_id)
    req_ids = [r.id for r in reqs]
    links = []
    if req_ids:
        rows = await db.execute(
            select(db_models.QualityRequirementLink).where(
                db_models.QualityRequirementLink.requirement_id.in_(req_ids)
            )
        )
        links = list(rows.scalars().all())
    if case_kind:
        links = [l for l in links if (l.case_kind or "ui") == case_kind]
    case_ids = list({int(l.case_id) for l in links})
    cases = {}
    if case_ids:
        rows = await db.execute(select(db_models.TestCase).where(db_models.TestCase.id.in_(case_ids)))
        cases = {int(c.id): c for c in rows.scalars().all()}
    # 每个用例最新运行
    latest = {}
    if case_ids:
        rows = await db.execute(
            select(db_models.TestRun.case_id, db_models.TestRun.status, db_models.TestRun.id)
            .join(db_models.RunBatch, db_models.RunBatch.id == db_models.TestRun.batch_id)
            .where(db_models.TestRun.case_id.in_(case_ids))
            .order_by(db_models.TestRun.id.desc())
        )
        for cid, status, rid in rows.all():
            if cid is not None and int(cid) not in latest:
                latest[int(cid)] = status
    by_req: dict[int, list] = {}
    for link in links:
        by_req.setdefault(int(link.requirement_id), []).append(link)

    out_reqs = []
    uncovered = []
    for req in reqs:
        ls = by_req.get(req.id, [])
        passed = sum(1 for l in ls if latest.get(int(l.case_id)) in ("passed", "success"))
        failed = sum(1 for l in ls if latest.get(int(l.case_id)) in ("failed", "error"))
        coverage = "uncovered" if not ls else ("partial" if failed else "covered")
        if coverage == "uncovered":
            uncovered.append(req.id)
        out_reqs.append({
            "id": req.id, "name": req.name, "module_id": req.module_id,
            "source": req.source, "status": req.status,
            "case_count": len(ls), "passed": passed, "failed": failed,
            "coverage": coverage,
            "last_run_at": None,
        })
    out_reqs.sort(key=lambda r: (0 if r["coverage"] == "uncovered" else 1, r["name"]))
    linked_ids = set(case_ids)
    unlinked = []
    rows = await db.execute(
        select(db_models.TestCase.id, db_models.TestCase.name, db_models.TestCase.case_kind)
        .where(db_models.TestCase.project_id == project_id)
    )
    for cid, name, kind in rows.all():
        if int(cid) not in linked_ids:
            unlinked.append({"case_id": int(cid), "name": name, "case_kind": kind or "ui"})
    return {
        "requirements": out_reqs,
        "uncovered": uncovered,
        "unlinked_cases": unlinked[:200],
        "case_kind": case_kind or "all",
    }


async def link_cases(db: AsyncSession, *, requirement_id: int, case_ids: list[int],
                     source: str, user_id: int | None) -> int:
    count = 0
    for cid in case_ids:
        case = await db.get(db_models.TestCase, int(cid))
        if case is None:
            continue
        await cq.link_case(
            db, requirement_id=requirement_id, case_id=int(cid),
            case_kind=case.case_kind or "ui", source=source, created_by=user_id,
        )
        count += 1
    return count
