# app/models/suite.py
# 测试用例集 ORM 模型（项目内有序用例清单）
from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey, UniqueConstraint
from sqlalchemy.orm import relationship

from app.database import Base
from app.tz import now as tz_now


class TestSuite(Base):
    __tablename__ = "test_suites"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False, index=True)
    name = Column(String, nullable=False)
    description = Column(Text, nullable=True)
    # 对齐用例：functional | ui
    case_kind = Column(String(32), default="ui", nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), default=tz_now)
    updated_at = Column(DateTime(timezone=True), default=tz_now, onupdate=tz_now)

    cases = relationship(
        "TestSuiteCase",
        back_populates="suite",
        cascade="all, delete-orphan",
        order_by="TestSuiteCase.order_index",
        lazy="selectin",
    )


class TestSuiteCase(Base):
    __tablename__ = "test_suite_cases"
    __table_args__ = (
        UniqueConstraint("suite_id", "case_id", name="uq_suite_case"),
    )

    id = Column(Integer, primary_key=True, index=True)
    suite_id = Column(Integer, ForeignKey("test_suites.id", ondelete="CASCADE"), nullable=False, index=True)
    case_id = Column(Integer, ForeignKey("test_cases.id", ondelete="CASCADE"), nullable=False, index=True)
    order_index = Column(Integer, nullable=False, default=0)

    suite = relationship("TestSuite", back_populates="cases")
