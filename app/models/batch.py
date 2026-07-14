# app/models/batch.py
# 运行批次、用例执行、执行日志 ORM 模型
from sqlalchemy import Column, Integer, String, Text, Boolean, DateTime, Float, ForeignKey
from sqlalchemy.orm import relationship

from app.database import Base
from app.tz import now as tz_now


class RunBatch(Base):
    """运行批次 - 一次批量运行的容器"""
    __tablename__ = "run_batches"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False, index=True)
    name = Column(String, default="")
    status = Column(String, nullable=False, default="running")
    total_cases = Column(Integer, default=0)
    passed = Column(Integer, default=0)
    failed = Column(Integer, default=0)
    created_at = Column(DateTime(timezone=True), default=tz_now)
    started_at = Column(DateTime(timezone=True), nullable=True)
    finished_at = Column(DateTime(timezone=True), nullable=True)
    triggered_by = Column(String(255), nullable=True)

    runs = relationship("TestRun", backref="batch")


class TestRun(Base):
    __tablename__ = "test_runs"

    id = Column(Integer, primary_key=True, index=True)
    case_id = Column(Integer, ForeignKey("test_cases.id"), nullable=False, index=True)
    batch_id = Column(Integer, ForeignKey("run_batches.id"), nullable=True, index=True)
    status = Column(String, nullable=False)
    # nullable: 预创建 pending 行留空，避开 _compute_batch_status 卡死检查误判
    start_time = Column(DateTime(timezone=True), nullable=True)
    end_time = Column(DateTime(timezone=True), nullable=True)
    duration = Column(Float, nullable=True)
    report_path = Column(String, nullable=True)
    log_path = Column(String, nullable=True)
    execution_mode = Column(String, default="server", nullable=False)
    is_init = Column(Boolean, default=False, nullable=False)  # 是否初始化用例


class RunLog(Base):
    __tablename__ = "run_logs"

    id = Column(Integer, primary_key=True, index=True)
    run_id = Column(Integer, ForeignKey("test_runs.id"), nullable=False, index=True)
    step_id = Column(Integer, ForeignKey("test_steps.id", ondelete="SET NULL"), nullable=True, index=True)
    timestamp = Column(DateTime(timezone=True), default=tz_now)
    level = Column(String, nullable=False)
    message = Column(Text, nullable=False)
    screenshot_path = Column(String, nullable=True)
