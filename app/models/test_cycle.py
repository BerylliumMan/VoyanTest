# app/models/test_cycle.py
# 050 测试管理闭环：测试轮次 / 轮次项 ORM 模型
from sqlalchemy import (
    Column, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint,
)

from app.database import Base
from app.tz import now as tz_now


class TestCycle(Base):
    """测试轮次 - 计划的一次执行；启动后生成一个运行批次"""

    __tablename__ = "test_cycles"

    id = Column(Integer, primary_key=True, index=True)
    plan_id = Column(Integer, ForeignKey("test_plans.id", ondelete="CASCADE"), nullable=False, index=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False, index=True)
    name = Column(String(200), nullable=False)
    environment_id = Column(Integer, ForeignKey("environments.id"), nullable=True)
    agent_id = Column(Integer, ForeignKey("agent_definitions.id"), nullable=True)
    # planned / running / completed / cancelled
    status = Column(String(16), nullable=False, default="planned", index=True)
    # 最新批次（重跑后指向新批次）
    batch_id = Column(Integer, ForeignKey("run_batches.id"), nullable=True)
    case_count = Column(Integer, nullable=False, default=0)
    skipped_count = Column(Integer, nullable=False, default=0)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    started_at = Column(DateTime(timezone=True), nullable=True)
    finished_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=tz_now, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=tz_now, onupdate=tz_now, nullable=False)


class TestCycleItem(Base):
    """轮次项 - 创建轮次时的用例快照（此后计划项变化不影响本轮）"""

    __tablename__ = "test_cycle_items"
    __table_args__ = (
        UniqueConstraint("cycle_id", "case_id", name="uq_test_cycle_items_cycle_case"),
    )

    id = Column(Integer, primary_key=True, index=True)
    cycle_id = Column(Integer, ForeignKey("test_cycles.id", ondelete="CASCADE"), nullable=False, index=True)
    plan_item_id = Column(Integer, ForeignKey("test_plan_items.id", ondelete="SET NULL"), nullable=True)
    case_id = Column(Integer, ForeignKey("test_cases.id", ondelete="SET NULL"), nullable=True, index=True)
    case_name = Column(String(255), nullable=False)
    case_kind = Column(String(16), nullable=False, default="functional")
    assignee_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    sort_order = Column(Integer, nullable=False, default=0)
