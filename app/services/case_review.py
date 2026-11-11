"""050 US5：用例人工评审服务（状态机、pending 唯一、通知）。"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import db_models
from app.services.test_plan import ensure_project_access
from app.tz import now as tz_now

REVIEW_STATUSES = {"pending", "approved", "changes_requested", "rejected", "cancelled"}
DECISION_STATUSES = {"approved", "changes_requested", "rejected"}


def _name(user) -> str:
    return getattr(user, "nickname", None) or getattr(user, "username", "") or ""


def _review_dict(review, requester=None, reviewer=None, case=None) -> dict[str, Any]:
    return {
        "id": review.id,
        "project_id": review.project_id,
        "case_id": review.case_id,
        "case_name": getattr(case, "name", None),
        "requester_id": review.requester_id,
        "requester_name": _name(requester) if requester is not None else "",
        "reviewer_id": review.reviewer_id,
        "reviewer_name": _name(reviewer) if reviewer is not None else "",
        "status": review.status,
        "note": review.note,
        "comment": review.comment,
        "created_at": review.created_at,
        "decided_at": review.decided_at,
        "updated_at": review.updated_at,
    }


async def _load_users(db: AsyncSession, *ids: int | None) -> dict[int, db_models.User]:
    wanted = {int(i) for i in ids if i}
    if not wanted:
        return {}
    rows = (
        await db.execute(select(db_models.User).where(db_models.User.id.in_(wanted)))
    ).scalars().all()
    return {int(u.id): u for u in rows}


async def create_review(
    db: AsyncSession, user, *, case_id: int, reviewer_id: int, note: str | None = None
) -> dict[str, Any]:
    case = await db.get(db_models.TestCase, int(case_id))
    if case is None:
        raise HTTPException(status_code=404, detail="用例不存在")
    ensure_project_access(user, case.project_id)
    reviewer = await db.get(db_models.User, int(reviewer_id))
    if reviewer is None or getattr(reviewer, "status", "active") == "disabled":
        raise HTTPException(status_code=400, detail="评审人不存在或已禁用")
    if int(reviewer_id) == int(user.id):
        raise HTTPException(status_code=400, detail="不能评审自己发起的用例")
    ensure_project_access(reviewer, case.project_id)

    pending = (
        await db.execute(
            select(db_models.CaseReview).where(
                db_models.CaseReview.case_id == case.id,
                db_models.CaseReview.status == "pending",
            )
        )
    ).scalar_one_or_none()
    if pending is not None:
        raise HTTPException(status_code=409, detail="该用例已有待处理评审")

    review = db_models.CaseReview(
        project_id=case.project_id,
        case_id=case.id,
        requester_id=int(user.id),
        reviewer_id=int(reviewer_id),
        status="pending",
        note=note,
    )
    db.add(review)
    await db.flush()
    db.add(
        db_models.Notification(
            user_id=int(reviewer_id),
            type="info",
            title="你有一个待评审的用例",
            message=f"{_name(user)} 请你评审用例「{case.name}」",
        )
    )
    await db.commit()
    await db.refresh(review)
    return _review_dict(review, requester=user, reviewer=reviewer, case=case)


async def list_reviews(
    db: AsyncSession,
    user,
    *,
    status: str | None = None,
    mine_pending: bool = False,
    case_id: int | None = None,
    project_id: int | None = None,
) -> list[dict[str, Any]]:
    stmt = select(db_models.CaseReview)
    if project_id is not None:
        ensure_project_access(user, project_id)
        stmt = stmt.where(db_models.CaseReview.project_id == int(project_id))
    elif getattr(user, "role", None) != "admin":
        allowed = [int(x) for x in (getattr(user, "project_ids", None) or [])]
        stmt = stmt.where(db_models.CaseReview.project_id.in_(allowed))
    if status:
        if status not in REVIEW_STATUSES:
            raise HTTPException(status_code=422, detail="非法的评审状态")
        stmt = stmt.where(db_models.CaseReview.status == status)
    if mine_pending:
        stmt = stmt.where(
            db_models.CaseReview.reviewer_id == int(user.id),
            db_models.CaseReview.status == "pending",
        )
    if case_id is not None:
        stmt = stmt.where(db_models.CaseReview.case_id == int(case_id))
    stmt = stmt.order_by(db_models.CaseReview.created_at.desc())
    reviews = (await db.execute(stmt)).scalars().all()

    users = await _load_users(
        db,
        *{r.requester_id for r in reviews},
        *{r.reviewer_id for r in reviews},
    )
    case_ids = {int(r.case_id) for r in reviews}
    cases = {}
    if case_ids:
        rows = (
            await db.execute(select(db_models.TestCase).where(db_models.TestCase.id.in_(case_ids)))
        ).scalars().all()
        cases = {int(c.id): c for c in rows}
    return [
        _review_dict(
            r,
            requester=users.get(int(r.requester_id)),
            reviewer=users.get(int(r.reviewer_id)),
            case=cases.get(int(r.case_id)),
        )
        for r in reviews
    ]


async def decide_review(
    db: AsyncSession, user, review_id: int, *, status: str, comment: str | None = None
) -> dict[str, Any]:
    review = await db.get(db_models.CaseReview, int(review_id))
    if review is None:
        raise HTTPException(status_code=404, detail="评审不存在")
    if int(review.reviewer_id) != int(user.id):
        raise HTTPException(status_code=403, detail="只有评审人可以给出结论")
    if review.status != "pending":
        raise HTTPException(status_code=409, detail="该评审已处理")
    if status not in DECISION_STATUSES:
        raise HTTPException(status_code=422, detail="非法的评审结论")
    review.status = status
    review.comment = comment
    review.decided_at = tz_now()
    review.updated_at = tz_now()

    case = await db.get(db_models.TestCase, review.case_id)
    db.add(
        db_models.Notification(
            user_id=review.requester_id,
            type="info",
            title="你的用例评审有结果了",
            message=f"用例「{getattr(case, 'name', review.case_id)}」：{status}",
        )
    )
    await db.commit()
    await db.refresh(review)
    users = await _load_users(db, review.requester_id, review.reviewer_id)
    return _review_dict(
        review,
        requester=users.get(int(review.requester_id)),
        reviewer=users.get(int(review.reviewer_id)),
        case=case,
    )


async def cancel_review(db: AsyncSession, user, review_id: int) -> dict[str, Any]:
    review = await db.get(db_models.CaseReview, int(review_id))
    if review is None:
        raise HTTPException(status_code=404, detail="评审不存在")
    if getattr(user, "role", None) != "admin" and int(review.requester_id) != int(user.id):
        raise HTTPException(status_code=403, detail="只有发起人可以取消")
    if review.status != "pending":
        raise HTTPException(status_code=409, detail="该评审已处理")
    review.status = "cancelled"
    review.decided_at = tz_now()
    review.updated_at = tz_now()
    await db.commit()
    await db.refresh(review)
    users = await _load_users(db, review.requester_id, review.reviewer_id)
    case = await db.get(db_models.TestCase, review.case_id)
    return _review_dict(
        review,
        requester=users.get(int(review.requester_id)),
        reviewer=users.get(int(review.reviewer_id)),
        case=case,
    )


async def list_case_reviews(db: AsyncSession, user, case_id: int) -> list[dict[str, Any]]:
    case = await db.get(db_models.TestCase, int(case_id))
    if case is None:
        raise HTTPException(status_code=404, detail="用例不存在")
    ensure_project_access(user, case.project_id)
    return await list_reviews(db, user, case_id=int(case_id), project_id=case.project_id)


__all__ = [
    "REVIEW_STATUSES", "DECISION_STATUSES", "create_review", "list_reviews",
    "decide_review", "cancel_review", "list_case_reviews",
]
