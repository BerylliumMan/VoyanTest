# app/models/case_review.py
# 050 测试管理闭环：用例人工评审 ORM 模型
from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text

from app.database import Base
from app.tz import now as tz_now


class CaseReview(Base):
    """用例评审 - 发起人指定评审人，pending 期间同一用例唯一"""

    __tablename__ = "case_reviews"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False, index=True)
    case_id = Column(Integer, ForeignKey("test_cases.id", ondelete="CASCADE"), nullable=False, index=True)
    requester_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    reviewer_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    # pending / approved / changes_requested / rejected / cancelled
    status = Column(String(32), nullable=False, default="pending", index=True)
    note = Column(Text, nullable=True)
    comment = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), default=tz_now, nullable=False)
    decided_at = Column(DateTime(timezone=True), nullable=True)
    updated_at = Column(DateTime(timezone=True), default=tz_now, onupdate=tz_now, nullable=False)
