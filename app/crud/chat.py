"""对话工作台 CRUD（034-chat-case-workbench）。

设计约定（data-model.md / contracts C1）：
- 会话用户级私有：读/写一律带 ``user_id`` 所有权过滤；
- 消息按 id 升序分页；卡片状态原地更新（``update_message_cards``）；
- 变更记录（CaseRevision）只增不改。
"""
from typing import Optional

from sqlalchemy import select

from app import db_models
from app.database import AsyncSession


# ----------------------------
# Conversations
# ----------------------------

async def create_conversation(
    db: AsyncSession,
    *,
    user_id: int,
    project_id: Optional[int] = None,
    assistant_agent_id: Optional[int] = None,
    title: str = "新对话",
    refs: Optional[list] = None,
    case_kind: Optional[str] = None,
    environment_id: Optional[int] = None,
    agent_id: Optional[int] = None,
) -> db_models.ChatConversation:
    record = db_models.ChatConversation(
        title=(title or "新对话")[:200],
        user_id=user_id,
        project_id=project_id,
        assistant_agent_id=assistant_agent_id,
        status="active",
        refs=refs or None,
        case_kind=(case_kind or "ui"),
        environment_id=environment_id,
        agent_id=agent_id,
    )
    db.add(record)
    await db.commit()
    await db.refresh(record)
    return record


async def get_conversation(
    db: AsyncSession,
    conversation_id: int,
    *,
    user_id: Optional[int] = None,
) -> Optional[db_models.ChatConversation]:
    """按 id 获取会话；``user_id`` 非空时强制所有权过滤（用户级私有）。"""
    stmt = select(db_models.ChatConversation).where(
        db_models.ChatConversation.id == conversation_id
    )
    if user_id is not None:
        stmt = stmt.where(db_models.ChatConversation.user_id == user_id)
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def list_conversations(
    db: AsyncSession,
    user_id: int,
    *,
    status: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    base = select(db_models.ChatConversation).where(
        db_models.ChatConversation.user_id == user_id
    )
    if status:
        base = base.where(db_models.ChatConversation.status == status)
    base = base.order_by(db_models.ChatConversation.updated_at.desc())
    items = (
        await db.execute(base.offset(max(0, offset)).limit(max(1, min(200, limit))))
    ).scalars().all()
    return {"items": items}


async def update_conversation(
    db: AsyncSession,
    conversation: db_models.ChatConversation,
    *,
    preserve_updated_at: bool = False,
    **fields,
) -> db_models.ChatConversation:
    """部分更新：仅写入显式给出的字段。

    ``preserve_updated_at``：上下文类修改（agent/environment/case_kind）不触碰
    updated_at —— 会话列表按 updated_at 排序，自动绑定客户端不应让列表重排
    （2026-10-10 用户实测：浏览会话时列表不断跳动）。
    """
    allowed = {
        "title", "project_id", "assistant_agent_id", "status",
        "refs", "summary", "summarized_until_message_id",
        "case_kind", "environment_id", "agent_id",
    }
    _old_updated = conversation.updated_at
    for key, value in fields.items():
        if key not in allowed:
            continue
        if key == "title" and value is not None:
            value = str(value)[:200]
        setattr(conversation, key, value)
    if preserve_updated_at and _old_updated is not None:
        # 仅显式 setattr 不足以跳过 onupdate：当值未变化时 SQLAlchemy 不把该列
        # 纳入 UPDATE，onupdate 仍会刷新时间戳（2026-10-10 实测）——
        # 用 Core 显式 UPDATE 强制写回原值。
        from sqlalchemy import update as _sa_update

        await db.flush()
        await db.execute(
            _sa_update(type(conversation))
            .where(type(conversation).id == conversation.id)
            .values(updated_at=_old_updated)
        )
    await db.commit()
    await db.refresh(conversation)
    return conversation


async def delete_conversation(
    db: AsyncSession,
    conversation: db_models.ChatConversation,
) -> None:
    await db.delete(conversation)
    await db.commit()


# ----------------------------
# Messages
# ----------------------------

async def create_message(
    db: AsyncSession,
    *,
    conversation_id: int,
    role: str,
    content: Optional[str] = None,
    thinking: Optional[str] = None,
    cards: Optional[list] = None,
    refs: Optional[list] = None,
    actions: Optional[list] = None,
    task_id: Optional[str] = None,
) -> db_models.ChatMessage:
    record = db_models.ChatMessage(
        conversation_id=conversation_id,
        role=role,
        content=content,
        thinking=thinking,
        cards=cards or None,
        refs=refs or None,
        actions=actions or None,
        task_id=task_id,
    )
    db.add(record)
    await db.commit()
    await db.refresh(record)
    return record


async def get_message(
    db: AsyncSession,
    conversation_id: int,
    message_id: int,
) -> Optional[db_models.ChatMessage]:
    result = await db.execute(
        select(db_models.ChatMessage).where(
            db_models.ChatMessage.id == message_id,
            db_models.ChatMessage.conversation_id == conversation_id,
        )
    )
    return result.scalar_one_or_none()


async def list_messages(
    db: AsyncSession,
    conversation_id: int,
    *,
    before_id: Optional[int] = None,
    limit: int = 50,
) -> dict:
    """倒序取一页（返回按 id 升序），``has_more`` 表示更早还有。"""
    stmt = select(db_models.ChatMessage).where(
        db_models.ChatMessage.conversation_id == conversation_id
    )
    if before_id is not None:
        stmt = stmt.where(db_models.ChatMessage.id < before_id)
    stmt = stmt.order_by(db_models.ChatMessage.id.desc()).limit(max(1, min(200, limit)))
    rows = (await db.execute(stmt)).scalars().all()
    has_more = False
    if rows:
        oldest = rows[-1].id
        probe = await db.execute(
            select(db_models.ChatMessage.id)
            .where(
                db_models.ChatMessage.conversation_id == conversation_id,
                db_models.ChatMessage.id < oldest,
            )
            .limit(1)
        )
        has_more = probe.scalar_one_or_none() is not None
    return {"items": list(reversed(rows)), "has_more": has_more}


async def list_messages_after(
    db: AsyncSession,
    conversation_id: int,
    *,
    after_id: int = 0,
    limit: int = 500,
) -> list[db_models.ChatMessage]:
    stmt = (
        select(db_models.ChatMessage)
        .where(
            db_models.ChatMessage.conversation_id == conversation_id,
            db_models.ChatMessage.id > after_id,
        )
        .order_by(db_models.ChatMessage.id.asc())
        .limit(max(1, min(1000, limit)))
    )
    return (await db.execute(stmt)).scalars().all()


async def update_message_cards(
    db: AsyncSession,
    message: db_models.ChatMessage,
    cards: Optional[list],
) -> db_models.ChatMessage:
    """卡片原地更新（任务进度/结果回填）。"""
    message.cards = cards or None
    await db.commit()
    await db.refresh(message)
    return message


async def update_message(
    db: AsyncSession,
    message: db_models.ChatMessage,
    *,
    content: Optional[str] = None,
    thinking: Optional[str] = None,
    cards: Optional[list] = None,
    actions: Optional[list] = None,
) -> db_models.ChatMessage:
    """助手消息整条替换（重新生成用）：仅写入显式给出的字段。"""
    if content is not None:
        message.content = content
    if thinking is not None:
        message.thinking = thinking
    if cards is not None:
        message.cards = cards
    if actions is not None:
        message.actions = actions
    await db.commit()
    await db.refresh(message)
    return message


async def delete_message(
    db: AsyncSession,
    message: db_models.ChatMessage,
) -> None:
    await db.delete(message)
    await db.commit()


# ----------------------------
# Case revisions
# ----------------------------

async def create_case_revision(
    db: AsyncSession,
    *,
    case_id: int,
    user_id: int,
    source: str,
    conversation_id: Optional[int],
    before_snapshot: dict,
    after_snapshot: dict,
    summary: Optional[str] = None,
) -> db_models.CaseRevision:
    record = db_models.CaseRevision(
        case_id=case_id,
        user_id=user_id,
        source=source,
        conversation_id=conversation_id,
        before_snapshot=before_snapshot,
        after_snapshot=after_snapshot,
        summary=summary,
    )
    db.add(record)
    await db.commit()
    await db.refresh(record)
    return record


async def list_case_revisions(
    db: AsyncSession,
    case_id: int,
    *,
    limit: int = 50,
) -> list[db_models.CaseRevision]:
    stmt = (
        select(db_models.CaseRevision)
        .where(db_models.CaseRevision.case_id == case_id)
        .order_by(db_models.CaseRevision.id.desc())
        .limit(max(1, min(200, limit)))
    )
    return (await db.execute(stmt)).scalars().all()
