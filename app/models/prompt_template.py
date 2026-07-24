# app/models/prompt_template.py
# 提示词模板 ORM 模型（支持版本化管理）
from sqlalchemy import Column, Integer, String, Text, Boolean, DateTime, JSON, UniqueConstraint

from app.database import Base
from app.tz import now as tz_now


class PromptTemplate(Base):
    """提示词模板 — 支持多版本管理和按 key 检索。

    key 为模板标识（如 fp_extract、tc_generate），version 递增，
    (key, version) 联合唯一，每次编辑会创建新版本。
    """
    __tablename__ = "prompt_templates"

    __table_args__ = (
        UniqueConstraint('key', 'version'),
    )

    id = Column(Integer, primary_key=True)
    key = Column(String(100), nullable=False, index=True)
    name = Column(String(200), nullable=False)
    category = Column(String(50), nullable=False)
    content = Column(Text, nullable=False)
    variables = Column(JSON, default=list)
    version = Column(Integer, default=1)
    is_active = Column(Boolean, default=False)
    description = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), default=tz_now)
    updated_at = Column(DateTime(timezone=True), default=tz_now, onupdate=tz_now)
