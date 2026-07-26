# app/crud/agent_definition.py — AgentDefinition CRUD
#
# 提供对 agent_definitions 表的纯数据库操作。
# 特殊逻辑：is_active 同类型互斥，由 update_agent_definition 在事务中保证。
import logging
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app import db_models
from app.models.schemas import AgentDefinitionCreate, AgentDefinitionUpdate

logger = logging.getLogger(__name__)


async def list_agent_definitions(
    db: AsyncSession, agent_type: str | None = None
) -> list[db_models.AgentDefinition]:
    """列出 AgentDefinition，支持按 agent_type 过滤"""
    stmt = select(db_models.AgentDefinition)
    if agent_type:
        stmt = stmt.where(db_models.AgentDefinition.agent_type == agent_type)
    stmt = stmt.order_by(db_models.AgentDefinition.id)
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def get_agent_definition(
    db: AsyncSession, id: int
) -> db_models.AgentDefinition | None:
    """通过 ID 获取单个 AgentDefinition"""
    result = await db.execute(
        select(db_models.AgentDefinition).where(db_models.AgentDefinition.id == id)
    )
    return result.scalar_one_or_none()


async def create_agent_definition(
    db: AsyncSession, data: AgentDefinitionCreate
) -> db_models.AgentDefinition:
    """创建 AgentDefinition，name 唯一性校验（冲突 raise ValueError）"""
    existing = await db.execute(
        select(db_models.AgentDefinition).where(
            db_models.AgentDefinition.name == data.name
        )
    )
    if existing.scalar_one_or_none() is not None:
        raise ValueError(f"AgentDefinition name '{data.name}' already exists")

    db_obj = db_models.AgentDefinition(
        name=data.name,
        agent_type=data.agent_type,
        description=data.description,
        skills=data.skills,
        llm_config=data.llm_config,
        prompt_overrides=data.prompt_overrides,
        system_prompt=data.system_prompt,
        tools=data.tools,
        goal=data.goal,
        constraints=data.constraints,
        thinking_config=data.thinking_config,
        is_active=1 if data.is_active else 0,
    )
    db.add(db_obj)
    await db.commit()
    await db.refresh(db_obj)
    return db_obj


# generation 允许多个同时启用（功能用例 / UI 自动化）；execution/recording 仍互斥
_EXCLUSIVE_ACTIVE_TYPES = frozenset({"execution", "recording"})


async def update_agent_definition(
    db: AsyncSession, id: int, data: AgentDefinitionUpdate
) -> db_models.AgentDefinition | None:
    """部分更新 AgentDefinition。

    特殊规则：
    1. 若 is_active=True 且 agent_type 发生变更，强制 is_active=False（安全阀）
    2. is_active 互斥：仅 execution / recording 在激活时关闭同类型其他项；
       generation 可同时启用多个（由生成页按 agent_id 选择）
    3. 以上操作在同一事务中原子执行
    """
    db_obj = await get_agent_definition(db, id)
    if db_obj is None:
        return None

    changes = data.model_dump(exclude_unset=True)

    # 判断 agent_type 是否变更
    new_agent_type = changes.get("agent_type", db_obj.agent_type)
    agent_type_changed = new_agent_type != db_obj.agent_type
    new_is_active = changes.get("is_active", bool(db_obj.is_active))

    # 规则 1：变更 agent_type 的同时不允许保持/置为 active
    if new_is_active and agent_type_changed:
        changes["is_active"] = False
        logger.info(
            "AgentDefinition id=%d: agent_type 由 '%s' 变更为 '%s'，强制 is_active=False",
            id,
            db_obj.agent_type,
            new_agent_type,
        )
        new_is_active = False

    # 规则 2：execution/recording 互斥；generation 允许多活跃
    if new_is_active and new_agent_type in _EXCLUSIVE_ACTIVE_TYPES:
        await db.execute(
            update(db_models.AgentDefinition)
            .where(
                db_models.AgentDefinition.agent_type == new_agent_type,
                db_models.AgentDefinition.id != id,
            )
            .values(is_active=0)
        )

    # 应用字段变更
    for key, value in changes.items():
        setattr(db_obj, key, value)

    await db.commit()
    await db.refresh(db_obj)
    return db_obj


async def delete_agent_definition(
    db: AsyncSession, id: int
) -> db_models.AgentDefinition | None:
    """删除 AgentDefinition，返回被删记录或 None"""
    db_obj = await get_agent_definition(db, id)
    if db_obj is None:
        return None

    await db.delete(db_obj)
    await db.commit()
    return db_obj


async def get_active_by_type(
    db: AsyncSession, agent_type: str
) -> db_models.AgentDefinition | None:
    """获取指定类型当前激活的 AgentDefinition（每个类型最多一个激活项）"""
    result = await db.execute(
        select(db_models.AgentDefinition)
        .where(
            db_models.AgentDefinition.agent_type == agent_type,
            db_models.AgentDefinition.is_active == 1,
        )
        .order_by(db_models.AgentDefinition.id)
        .limit(1)
    )
    return result.scalar_one_or_none()
