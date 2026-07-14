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
    updated_at = Column(DateTime(timezone=True), default=tz_now, onupdate=tz_now)


class PromptTemplate(Base):
    """提示词模板 — AI 生成功能点和测试用例的 prompt。

    on first startup, 从 analyzer.py 代码常量和 seed 数据创建默认行。
    """
    __tablename__ = "prompt_templates"

    id = Column(Integer, primary_key=True, index=True)
    template_key = Column(String(100), unique=True, nullable=False, index=True)  # fp_extract, tc_generate
    label = Column(String(200), nullable=False)  # display name
    template_content = Column(Text, nullable=False)
    is_custom = Column(Boolean, default=False)  # True if user modified it
    updated_at = Column(DateTime(timezone=True), default=tz_now, onupdate=tz_now)
