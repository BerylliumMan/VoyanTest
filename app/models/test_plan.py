# app/models/test_plan.py
# 050 测试管理闭环：测试计划 / 计划项 ORM 模型
from sqlalchemy import (
    Column, Date, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint,
)

from app.database import Base
from app.tz import now as tz_now


class TestPlan(Base):
    """测试计划 - 一轮回归工作的容器（050）"""

    __tablename__ = "test_plans"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False, index=True)
    name = Column(String(200), nullable=False)
    description = Column(Text, nullable=True)
    # 可选关联 042 质量需求；不做强外键级联，需求删除时保留计划
    requirement_id = Column(Integer, ForeignKey("quality_requirements.id"), nullable=True)
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    # draft / active / completed / archived
    status = Column(String(16), nullable=False, default="draft", index=True)
    start_date = Column(Date, nullable=True)
    due_date = Column(Date, nullable=True)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), default=tz_now, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=tz_now, onupdate=tz_now, nullable=False)


class TestPlanItem(Base):
    """计划项 - 计划内的一条用例（带用例名快照与负责人）"""

    __tablename__ = "test_plan_items"
    __table_args__ = (
        UniqueConstraint("plan_id", "case_id", name="uq_test_plan_items_plan_case"),
    )

    id = Column(Integer, primary_key=True, index=True)
    plan_id = Column(Integer, ForeignKey("test_plans.id", ondelete="CASCADE"), nullable=False, index=True)
    # 用例删除后置空，保留名称/类型快照
    case_id = Column(Integer, ForeignKey("test_cases.id", ondelete="SET NULL"), nullable=True, index=True)
    case_name = Column(String(255), nullable=False)
    case_kind = Column(String(16), nullable=False, default="functional")
    assignee_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    # low / medium / high / critical
    priority = Column(String(16), nullable=False, default="medium")
    due_date = Column(Date, nullable=True, index=True)
    # not_started / in_progress / passed / failed / blocked
    status = Column(String(16), nullable=False, default="not_started", index=True)
    note = Column(Text, nullable=True)
    sort_order = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime(timezone=True), default=tz_now, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=tz_now, onupdate=tz_now, nullable=False)
