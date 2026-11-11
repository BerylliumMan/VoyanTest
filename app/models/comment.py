# app/models/comment.py
# 050 测试管理闭环：通用评论 ORM 模型（v1 只用于用例）
from sqlalchemy import Column, DateTime, ForeignKey, Integer, JSON, String, Text

from app.database import Base
from app.tz import now as tz_now


class Comment(Base):
    __tablename__ = "comments"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False, index=True)
    # 预留通用实体；v1 固定 'case'
    entity_type = Column(String(16), nullable=False, default="case", index=True)
    entity_id = Column(Integer, nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    body = Column(Text, nullable=False)
    # 被提及用户 id 列表
    mentions = Column(JSON, nullable=True, default=list)
    created_at = Column(DateTime(timezone=True), default=tz_now, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=tz_now, onupdate=tz_now, nullable=False)
