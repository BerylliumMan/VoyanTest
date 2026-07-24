# app/crud/agent_run.py — AgentRun CRUD
#
# 提供对 agent_runs / agent_messages 表的纯数据库操作。
import logging
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app import db_models
from app.models.schemas import AgentRunCreate
from app.models.agent_run import AgentRun, AgentMessage

logger = logging.getLogger(__name__)


async def list_agent_runs(
    db: AsyncSession,
    status: str | None = None,
    agent_definition_id: int | None = None,
    page: int = 1,
    size: int = 20,
) -> tuple[list[AgentRun], int]:
    """列出 AgentRun，支持按 status/agent_definition_id 过滤和分页"""
    stmt = select(db_models.AgentRun, db_models.AgentDefinition.name)
    stmt = stmt.outerjoin(
        db_models.AgentDefinition,
        db_models.AgentRun.agent_definition_id == db_models.AgentDefinition.id,
    )
    count_stmt = select(func.count()).select_from(db_models.AgentRun)

    if status:
        stmt = stmt.where(db_models.AgentRun.status == status)
        count_stmt = count_stmt.where(db_models.AgentRun.status == status)
    if agent_definition_id is not None:
        stmt = stmt.where(db_models.AgentRun.agent_definition_id == agent_definition_id)
        count_stmt = count_stmt.where(db_models.AgentRun.agent_definition_id == agent_definition_id)

    total_result = await db.execute(count_stmt)
    total = total_result.scalar() or 0

    offset = (page - 1) * size
    stmt = stmt.order_by(db_models.AgentRun.id.desc()).offset(offset).limit(size)
    result = await db.execute(stmt)
    items: list[AgentRun] = []
    for run_row in result.all():
        run = run_row[0]  # AgentRun 对象
        run._agent_name = run_row[1] or ""  # Agent 名称（可能为 None）
        items.append(run)

    return items, total


async def get_agent_run(db: AsyncSession, id: int) -> AgentRun | None:
    """通过 ID 获取单个 AgentRun"""
    stmt = select(db_models.AgentRun, db_models.AgentDefinition.name)
    stmt = stmt.outerjoin(
        db_models.AgentDefinition,
        db_models.AgentRun.agent_definition_id == db_models.AgentDefinition.id,
    )
    stmt = stmt.where(db_models.AgentRun.id == id)
    result = await db.execute(stmt)
    run_row = result.one_or_none()
    if run_row is None:
        return None
    run = run_row[0]  # AgentRun 对象
    run._agent_name = run_row[1] or ""  # Agent 名称
    return run


async def get_agent_run_messages(db: AsyncSession, run_id: int) -> list[AgentMessage]:
    """获取指定 run 的所有消息，按 turn_number 排序"""
    result = await db.execute(
        select(db_models.AgentMessage)
        .where(db_models.AgentMessage.run_id == run_id)
        .order_by(db_models.AgentMessage.turn_number)
    )
    return list(result.scalars().all())


async def create_agent_run(db: AsyncSession, data: AgentRunCreate) -> AgentRun:
    """创建新的 AgentRun"""
    db_obj = db_models.AgentRun(
        agent_definition_id=data.agent_definition_id,
        case_id=data.case_id,
        goal=data.goal,
    )
    db.add(db_obj)
    await db.commit()
    await db.refresh(db_obj)
    return db_obj


async def create_message(
    db: AsyncSession, run_id: int, turn_number: int, role: str, content: str
) -> AgentMessage:
    """为指定 run 创建一条 AgentMessage 记录"""
    obj = AgentMessage(
        run_id=run_id,
        turn_number=turn_number,
        role=role,
        content=content,
    )
    db.add(obj)
    await db.commit()
    return obj


async def create_tool_call(
    db: AsyncSession,
    run_id: int,
    turn_number: int,
    tool_name: str,
    tool_args: dict,
    success: bool,
    error: str = "",
) -> db_models.AgentToolCall:
    """为指定 run 创建一条 AgentToolCall 记录"""
    obj = db_models.AgentToolCall(
        run_id=run_id,
        turn_number=turn_number,
        tool_name=tool_name,
        tool_args=tool_args,
        success=1 if success else 0,
        error_message=error,
    )
    db.add(obj)
    await db.commit()
    return obj


async def update_agent_run_status(
    db: AsyncSession, id: int, status: str, **extra
) -> AgentRun | None:
    """更新 run 的 status 及可选的额外字段"""
    db_obj = await get_agent_run(db, id)
    if db_obj is None:
        return None

    db_obj.status = status
    for key, value in extra.items():
        setattr(db_obj, key, value)

    await db.commit()
    await db.refresh(db_obj)
    return db_obj
