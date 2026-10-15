"""035 US2：只读查询服务（数据问答直达；复用既有 crud + 双层权限）。

约定：
- 全部函数返回 ToolResult（有界 + 分页 + 截断标注）；
- 权限：项目过滤（get_user_project_filter；admin=None 放行）在服务层强制；
- 分页：limit 钳制 [1,100]，offset ≥ 0；结果含 total 与 offset，翻页用 offset+len(items)。
"""

from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app import db_models
from app.auth import get_user_project_filter
from core.tools.result import ToolResult, error_result, shape_result

logger = logging.getLogger(__name__)

DEFAULT_LIMIT = 20
MAX_LIMIT = 100
_BATCH_STATUSES = ("running", "passed", "failed", "cancelled", "paused")


def _clamp(limit, offset) -> tuple[int, int]:
    try:
        limit = int(limit) if limit is not None else DEFAULT_LIMIT
    except (TypeError, ValueError):
        limit = DEFAULT_LIMIT
    try:
        offset = int(offset) if offset is not None else 0
    except (TypeError, ValueError):
        offset = 0
    return max(1, min(limit, MAX_LIMIT)), max(0, offset)


async def allowed_projects(db, user_id) -> Optional[list[int]]:
    """用户可见项目 id 列表（admin → None=全部）。"""
    from app.crud.user import get_user_by_id

    user = await get_user_by_id(db, user_id)
    if user is None:
        return []
    return get_user_project_filter(user)


async def _check_project(db, user_id, project_id) -> tuple[bool, str]:
    try:
        pid = int(project_id)
    except (TypeError, ValueError):
        return False, f"非法项目 id：{project_id}"
    allowed = await allowed_projects(db, user_id)
    if allowed is not None and pid not in allowed:
        return False, f"无权访问项目 #{pid}"
    return True, ""


async def list_projects(db, user_id, *, query=None, limit=None, offset=None) -> ToolResult:
    limit, offset = _clamp(limit, offset)
    from app.crud import project as project_crud

    allowed = await allowed_projects(db, user_id)
    projects = await project_crud.list_projects_for_user(db, allowed)
    if query:
        q = str(query).strip().lower()
        projects = [
            p for p in projects
            if q in (p.name or "").lower() or q in (getattr(p, "description", "") or "").lower()
        ]
    total = len(projects)
    page = projects[offset:offset + limit]
    items = [
        {"id": p.id, "name": p.name, "description": (getattr(p, "description", "") or "")[:120]}
        for p in page
    ]
    return shape_result({"items": items, "total": total, "offset": offset},
                        summary=f"共 {total} 个项目，返回 {len(items)} 条")


async def search_cases(db, user_id, *, project_id, query=None, case_kind=None,
                       limit=None, offset=None) -> ToolResult:
    limit, offset = _clamp(limit, offset)
    ok, err = await _check_project(db, user_id, project_id)
    if not ok:
        return error_result(err)
    conditions = [db_models.TestCase.project_id == int(project_id)]
    if case_kind in ("ui", "functional", "api"):
        conditions.append(db_models.TestCase.case_kind == case_kind)
    if query:
        like = f"%{str(query)[:100]}%"
        conditions.append(
            db_models.TestCase.name.ilike(like) | db_models.TestCase.description.ilike(like)
        )
    total = (await db.execute(select(func.count(db_models.TestCase.id)).where(*conditions))).scalar_one()
    rows = (await db.execute(
        select(db_models.TestCase)
        .options(selectinload(db_models.TestCase.steps))
        .where(*conditions)
        .order_by(db_models.TestCase.id.desc())
        .offset(offset).limit(limit)
    )).scalars().all()
    items = [
        {
            "id": c.id, "name": c.name,
            "case_kind": getattr(c, "case_kind", "ui") or "ui",
            "steps": len(list(getattr(c, "steps", None) or [])),
        }
        for c in rows
    ]
    return shape_result({"items": items, "total": total, "offset": offset},
                        summary=f"共 {total} 条用例，返回 {len(items)} 条")


async def get_case(db, user_id, *, case_id) -> ToolResult:
    try:
        cid = int(case_id)
    except (TypeError, ValueError):
        return error_result(f"非法用例 id：{case_id}")
    case = await db.get(db_models.TestCase, cid)
    if case is None:
        return error_result(f"未找到用例 #{cid}")
    ok, err = await _check_project(db, user_id, case.project_id)
    if not ok:
        return error_result(err)
    from app.crud.testcase import get_steps_for_case

    steps = await get_steps_for_case(db, case.id)
    step_items = [
        {"order": s.step_order, "description": s.description, "expected": s.parsed_result or ""}
        for s in steps
    ]
    return shape_result(
        {
            "id": case.id, "name": case.name,
            "case_kind": getattr(case, "case_kind", "ui") or "ui",
            "project_id": case.project_id,
            "steps": step_items,
        },
        summary=f"用例 #{case.id}「{case.name}」共 {len(step_items)} 步",
    )


async def list_suites(db, user_id, *, project_id, case_kind=None,
                      limit=None, offset=None) -> ToolResult:
    limit, offset = _clamp(limit, offset)
    ok, err = await _check_project(db, user_id, project_id)
    if not ok:
        return error_result(err)
    from app.crud.suite import list_suites as _list_suites

    suites = await _list_suites(
        db, int(project_id), case_kind=case_kind if case_kind in ("ui", "functional", "api") else None,
    )
    total = len(suites)
    page = suites[offset:offset + limit]
    items = [
        {
            "id": s.id, "name": s.name,
            "case_kind": getattr(s, "case_kind", "ui") or "ui",
            "count": len(list(getattr(s, "cases", None) or [])),
        }
        for s in page
    ]
    return shape_result({"items": items, "total": total, "offset": offset},
                        summary=f"共 {total} 个用例集，返回 {len(items)} 条")


async def list_batches(db, user_id, *, project_id=None, status=None,
                       limit=None, offset=None) -> ToolResult:
    limit, offset = _clamp(limit, offset)
    if project_id is not None:
        ok, err = await _check_project(db, user_id, project_id)
        if not ok:
            return error_result(err)
    allowed = await allowed_projects(db, user_id)
    from app.crud import run as run_crud

    page_no = offset // limit + 1
    data = await run_crud.list_run_batches(
        db,
        project_id=int(project_id) if project_id is not None else None,
        status=status if status in _BATCH_STATUSES else None,
        page=page_no, size=limit, project_ids=allowed,
    )
    items = [
        {
            "id": b.id, "name": b.name or "", "status": b.status,
            "project_id": b.project_id,
            "total": b.total_cases, "passed": b.passed, "failed": b.failed,
            "created_at": b.created_at.isoformat() if b.created_at else None,
        }
        for b in data["items"]
    ]
    return shape_result({"items": items, "total": data["total"], "offset": offset},
                        summary=f"共 {data['total']} 个批次，返回 {len(items)} 条")


async def get_batch(db, user_id, *, batch_id) -> ToolResult:
    try:
        bid = int(batch_id)
    except (TypeError, ValueError):
        return error_result(f"非法批次 id：{batch_id}")
    batch = await db.get(db_models.RunBatch, bid)
    if batch is None:
        return error_result(f"未找到批次 #{bid}")
    ok, err = await _check_project(db, user_id, batch.project_id)
    if not ok:
        return error_result(err)
    from app.crud.run import get_batch_detail_with_related

    detail = await get_batch_detail_with_related(db, batch.id)
    runs = detail["runs"]
    cases = detail["cases"]
    run_items = [
        {
            "case_id": r.case_id,
            "case": (cases.get(r.case_id).name if cases.get(r.case_id) else ""),
            "status": r.status,
            "duration_s": round(float(r.duration or 0), 1),
        }
        for r in runs[:50]
    ]
    failed_runs = [x for x in run_items if x["status"] == "failed"]
    return shape_result(
        {
            "id": batch.id, "status": batch.status, "project_id": batch.project_id,
            "total": batch.total_cases, "passed": batch.passed, "failed": batch.failed,
            "runs": run_items, "failed_cases": [x["case_id"] for x in failed_runs],
        },
        summary=(
            f"批次 #{batch.id} 状态 {batch.status}：{batch.passed}/{batch.total_cases} 通过"
            + (f"，失败用例 {[x['case_id'] for x in failed_runs]}" if failed_runs else "")
        ),
    )
