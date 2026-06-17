# app/crud/agent.py - 分布式 Agent 与 Agent 日志 CRUD
#
# 提供对 Agent 与 AgentLog 表的纯数据库操作。
# 注意：online/offline 检测（看 last_heartbeat 时间窗 + WebSocket session）
# 属于业务逻辑，由 router 层负责，不在本文件中。
import logging
from sqlalchemy.orm import Session

from app import db_models, models
from app.tz import now as tz_now

logger = logging.getLogger(__name__)


# ----------------------------
# Agent CRUD
# ----------------------------

def get_agent(db: Session, agent_id: int) -> db_models.Agent | None:
    """通过ID获取 Agent"""
    return db.query(db_models.Agent).filter(db_models.Agent.id == agent_id).first()


def get_agent_by_name(db: Session, name: str) -> db_models.Agent | None:
    """通过名称获取 Agent"""
    return db.query(db_models.Agent).filter(db_models.Agent.name == name).first()


def list_agents(db: Session) -> list[db_models.Agent]:
    """获取所有 Agent（按 created_at 倒序）"""
    return db.query(db_models.Agent).order_by(db_models.Agent.created_at.desc()).all()


def create_agent(db: Session, agent_data: models.AgentCreate) -> db_models.Agent:
    """创建新 Agent，初始 status='offline'"""
    db_agent = db_models.Agent(
        name=agent_data.name,
        endpoint=agent_data.endpoint,
        description=agent_data.description,
        status="offline",
    )
    db.add(db_agent)
    db.commit()
    db.refresh(db_agent)
    return db_agent


def update_agent(
    db: Session,
    agent_id: int,
    update_data: models.AgentUpdate,
) -> db_models.Agent | None:
    """部分更新 Agent（仅更新传入的字段）

    返回更新后的 ORM 对象；若 Agent 不存在则返回 None，由 router 决定抛 404。
    """
    db_agent = get_agent(db, agent_id)
    if db_agent is None:
        return None

    # 仅更新传入的字段（exclude_unset 保留 PATCH 语义）
    changes = update_data.model_dump(exclude_unset=True)
    for key, value in changes.items():
        setattr(db_agent, key, value)

    db.commit()
    db.refresh(db_agent)
    return db_agent


def delete_agent(db: Session, agent_id: int) -> db_models.Agent | None:
    """删除 Agent 及该 Agent 的所有日志

    返回被删除的 ORM 对象；若 Agent 不存在则返回 None。
    先清日志再删 Agent，避免外键约束冲突。
    """
    db_agent = get_agent(db, agent_id)
    if db_agent is None:
        return None

    # 先删除该 Agent 的所有日志
    db.query(db_models.AgentLog).filter(
        db_models.AgentLog.agent_id == agent_id
    ).delete(synchronize_session=False)

    db.delete(db_agent)
    db.commit()
    return db_agent


def update_agent_heartbeat(db: Session, agent_id: int) -> db_models.Agent | None:
    """更新 Agent 的心跳时间并把 status 置为 online

    返回更新后的 ORM 对象；若 Agent 不存在则返回 None。
    """
    db_agent = get_agent(db, agent_id)
    if db_agent is None:
        return None

    db_agent.last_heartbeat = tz_now()
    db_agent.status = "online"
    db.commit()
    db.refresh(db_agent)
    return db_agent


# ----------------------------
# AgentLog CRUD
# ----------------------------

def list_agent_logs(
    db: Session,
    agent_id: int,
    page: int,
    size: int,
) -> dict:
    """分页获取指定 Agent 的日志（按 created_at 倒序）

    返回 dict: {items, total, page, size}
    若 Agent 不存在由 router 提前判断并返回 404，本函数不重复校验。
    """
    query = (
        db.query(db_models.AgentLog)
        .filter(db_models.AgentLog.agent_id == agent_id)
        .order_by(db_models.AgentLog.created_at.desc())
    )
    total = query.count()
    items = query.offset((page - 1) * size).limit(size).all()
    return {"items": items, "total": total, "page": page, "size": size}