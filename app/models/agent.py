# app/models/agent.py
# 分布式 Agent 与 Agent 日志 ORM 模型
from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey, JSON

from app.database import Base
from app.tz import now as tz_now


class Agent(Base):
    __tablename__ = "agents"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(255), nullable=False, unique=True)
    endpoint = Column(String(500), nullable=False)
    description = Column(Text, default="")
    status = Column(String(50), default="offline")
    last_heartbeat = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=tz_now)


class AgentLog(Base):
    __tablename__ = "agent_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    agent_id = Column(Integer, ForeignKey("agents.id"), nullable=False, index=True)
    level = Column(String(50), default="info")
    message = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), default=tz_now)


class AgentDefinition(Base):
    """服务端 AI Agent 定义 — 不同 Agent 负责不同任务域"""
    __tablename__ = "agent_definitions"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), unique=True, nullable=False, index=True)
    agent_type = Column(String(50), nullable=False, index=True)   # generation / execution / recording
    description = Column(Text, default="")
    skills = Column(JSON, default=list)                           # ["fp_extract","tc_generate",...]
    llm_config = Column(JSON, default=dict)                       # {"model":"qwen3.5","temperature":0.1}
    prompt_overrides = Column(JSON, default=dict)                 # {"fp_extract_key":"覆盖内容"}
    system_prompt = Column(Text, nullable=True)                    # Agent 全局系统提示词
    tools = Column(JSON, default=list)                            # [{"name":"browser_click","description":"...","enabled":true}]
    goal = Column(Text, default="")                                # Agent 目标描述
    constraints = Column(JSON, default=list)                       # [{"key":"url_whitelist","value":"*.example.com"}]
    thinking_config = Column(JSON, default=dict)                  # {"budget":4096,"strategy":"auto"}
    is_active = Column(Integer, default=1)
    created_at = Column(DateTime(timezone=True), default=tz_now)
