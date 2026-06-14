# app/models/agent.py
# 分布式 Agent 与 Agent 日志 ORM 模型
from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey

from app.database import Base
from app.tz import now as tz_now


class Agent(Base):
    __tablename__ = "agents"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(255), nullable=False, unique=True)
    endpoint = Column(String(500), nullable=False)
    description = Column(Text, default="")
    status = Column(String(50), default="offline")
    last_heartbeat = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=tz_now)


class AgentLog(Base):
    __tablename__ = "agent_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    agent_id = Column(Integer, ForeignKey("agents.id"), nullable=False, index=True)
    level = Column(String(50), default="info")
    message = Column(Text, nullable=False)
    created_at = Column(DateTime, default=tz_now)
