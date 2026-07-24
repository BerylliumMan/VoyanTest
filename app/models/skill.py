"""技能（Skill）模型 — 服务端 Agent 可配置的能力模块"""
from sqlalchemy import Column, Integer, String, Text, DateTime, JSON
from app.database import Base
from app.tz import now as tz_now


class Skill(Base):
    __tablename__ = "skills"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), unique=True, nullable=False, index=True)
    display_name = Column(String(200), nullable=False)
    category = Column(String(50), nullable=False, index=True)     # generation / execution / recording
    description = Column(Text, default="")
    prompt_key = Column(String(100), nullable=True)               # 关联的提示词模板 key
    required_client_caps = Column(JSON, default=list)             # 需要的客户端能力 ["mcp","playwright"]
    is_active = Column(Integer, default=1)
    created_at = Column(DateTime(timezone=True), default=tz_now)
