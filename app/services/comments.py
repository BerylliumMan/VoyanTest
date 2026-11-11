"""050 US4：用例评论服务（@ 提及解析、权限、通知）。"""

from __future__ import annotations

import re
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import db_models
from app.services.test_plan import ensure_project_access
from app.tz import now as tz_now

MENTION_RE = re.compile(r"@([A-Za-z0-9_\u4e00-\u9fa5.\-]{1,64})")
BODY_MAX = 5000
ENTITY_TYPES = {"case"}


def _display_name(user) -> str:
    return getattr(user, "nickname", None) or getattr(user, "username", "") or ""


async def _case_for_entity(db: AsyncSession, entity_type: str, entity_id: int) -> db_models.TestCase:
    if entity_type not in ENTITY_TYPES:
        raise HTTPException(status_code=422, detail="不支持的评论对象类型")
    case = await db.get(db_models.TestCase, int(entity_id))
    if case is None:
        raise HTTPException(status_code=404, detail="用例不存在")
    return case


def _clean_body(body: str | None) -> str:
    text = (body or "").strip()
    if not text:
        raise HTTPException(status_code=422, detail="评论内容不能为空")
    if len(text) > BODY_MAX:
        raise HTTPException(status_code=422, detail=f"评论不能超过 {BODY_MAX} 字")
    return text


async def _resolve_mentions(
    db: AsyncSession, text: str, explicit: list[int] | None = None
) -> tuple[list[int], list[db_models.User]]:
    names = MENTION_RE.findall(text or "")
    users: dict[int, db_models.User] = {}
    if names:
        rows = (
            await db.execute(
                select(db_models.User).where(
                    (db_models.User.username.in_(set(names)))
                    | (db_models.User.nickname.in_(set(names)))
                )
            )
        ).scalars().all()
        for u in rows:
            users[int(u.id)] = u
    if explicit:
        wanted = {int(x) for x in explicit}
        rows = (
            await db.execute(select(db_models.User).where(db_models.User.id.in_(wanted)))
        ).scalars().all()
        for u in rows:
            users[int(u.id)] = u
    ids = sorted(users.keys())
    return ids, list(users.values())


def _comment_dict(comment, author, user) -> dict[str, Any]:
    can_edit = getattr(user, "role", None) == "admin" or int(comment.user_id) == int(getattr(user, "id", 0))
    return {
        "id": comment.id,
        "entity_type": comment.entity_type,
        "entity_id": comment.entity_id,
        "body": comment.body,
        "user_id": comment.user_id,
        "user_name": _display_name(author) if author is not None else "",
        "mentions": comment.mentions or [],
        "can_edit": can_edit,
        "created_at": comment.created_at,
        "updated_at": comment.updated_at,
    }


async def list_comments(
    db: AsyncSession, user, *, entity_type: str = "case", entity_id: int
) -> list[dict[str, Any]]:
    case = await _case_for_entity(db, entity_type, entity_id)
    ensure_project_access(user, case.project_id)
    comments = (
        await db.execute(
            select(db_models.Comment)
            .where(
                db_models.Comment.entity_type == entity_type,
                db_models.Comment.entity_id == int(entity_id),
            )
            .order_by(db_models.Comment.created_at, db_models.Comment.id)
        )
    ).scalars().all()
    author_ids = {int(c.user_id) for c in comments}
    authors = {}
    if author_ids:
        rows = (
            await db.execute(select(db_models.User).where(db_models.User.id.in_(author_ids)))
        ).scalars().all()
        authors = {int(u.id): u for u in rows}
    return [_comment_dict(c, authors.get(int(c.user_id)), user) for c in comments]


async def create_comment(
    db: AsyncSession,
    user,
    *,
    entity_type: str,
    entity_id: int,
    body: str | None,
    mentions: list[int] | None = None,
) -> dict[str, Any]:
    case = await _case_for_entity(db, entity_type, entity_id)
    ensure_project_access(user, case.project_id)
    text = _clean_body(body)
    mention_ids, mentioned_users = await _resolve_mentions(db, text, mentions)

    comment = db_models.Comment(
        project_id=case.project_id,
        entity_type=entity_type,
        entity_id=int(entity_id),
        user_id=int(user.id),
        body=text,
        mentions=mention_ids,
    )
    db.add(comment)
    await db.flush()

    for target in mentioned_users:
        if int(target.id) == int(user.id):
            continue
        db.add(
            db_models.Notification(
                user_id=int(target.id),
                type="info",
                title=f"{_display_name(user)} 在用例评论中提到了你",
                message=f"用例「{case.name}」：{text[:100]}",
            )
        )
    await db.commit()
    await db.refresh(comment)
    return _comment_dict(comment, user, user)


async def update_comment(
    db: AsyncSession, user, comment_id: int, body: str | None
) -> dict[str, Any]:
    comment = await db.get(db_models.Comment, int(comment_id))
    if comment is None:
        raise HTTPException(status_code=404, detail="评论不存在")
    if getattr(user, "role", None) != "admin" and int(comment.user_id) != int(user.id):
        raise HTTPException(status_code=403, detail="只能编辑自己的评论")
    text = _clean_body(body)
    comment.body = text
    comment.mentions = (await _resolve_mentions(db, text))[0]
    comment.updated_at = tz_now()
    await db.commit()
    await db.refresh(comment)
    author = await db.get(db_models.User, comment.user_id)
    return _comment_dict(comment, author, user)


async def delete_comment(db: AsyncSession, user, comment_id: int) -> dict[str, Any]:
    comment = await db.get(db_models.Comment, int(comment_id))
    if comment is None:
        raise HTTPException(status_code=404, detail="评论不存在")
    if getattr(user, "role", None) != "admin" and int(comment.user_id) != int(user.id):
        raise HTTPException(status_code=403, detail="只能删除自己的评论")
    await db.delete(comment)
    await db.commit()
    return {"ok": True}


__all__ = [
    "MENTION_RE", "BODY_MAX", "list_comments", "create_comment",
    "update_comment", "delete_comment",
]
