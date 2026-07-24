# app/models/config.py
# AI 配置与提示词模板 ORM 模型
from sqlalchemy import Column, Integer, String, Text, Boolean, DateTime, Float

from app.database import Base
from app.tz import now as tz_now


class AIConfig(Base):
    """Single-row table holding the global AI model configuration.

    The first row (id=1) is created on first startup, seeded from
    config.json if present. All API and runner paths read this row.
    """
    __tablename__ = "ai_configs"

    id = Column(Integer, primary_key=True, default=1)
    model = Column(String(255), nullable=False)
    api_key = Column(String(500), nullable=False)
    api_base = Column(String(500), nullable=False)
    temperature = Column(Float, nullable=False, default=0.1)
    max_context_tokens = Column(Integer, nullable=False, default=131072)
    updated_at = Column(DateTime(timezone=True), default=tz_now, onupdate=tz_now)
