# app/models/batch.py
# 运行批次、用例执行、执行日志 ORM 模型
from sqlalchemy import Column, Integer, String, Text, Boolean, DateTime, Float, ForeignKey, JSON
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
    # 统计来源：api_scenario 表示接口场景执行，source_id 为场景 id
    source = Column(String(32), nullable=True, index=True)
    source_id = Column(Integer, nullable=True, index=True)
    # 「仅失败重跑」来源批次（031 US5）；NULL=普通批次
    parent_batch_id = Column(Integer, nullable=True, index=True)
    # 039：本批次展开的数据组总数（非参数化批次为 NULL）
    dataset_total = Column(Integer, nullable=True)
    # 042：本批次因 Flaky 隔离而跳过的用例数（不进入 total_cases）
    quarantined_count = Column(Integer, nullable=False, default=0)
    # 049：本批次是否由自动重试创建；为真时不再派生新的重试批次
    auto_retry = Column(Boolean, nullable=False, default=False)
    # 049：收尾时缓存的失败分类计数 {"env": 2, "assertion": 1}
    failure_summary = Column(JSON, nullable=True)

    runs = relationship("TestRun", backref="batch")


class TestRun(Base):
    __tablename__ = "test_runs"

    id = Column(Integer, primary_key=True, index=True)
    case_id = Column(Integer, ForeignKey("test_cases.id"), nullable=True, index=True)
    # 无对应用例的场景步骤（自定义请求 / 引用定义）在报告里显示的名称
    display_name = Column(String(255), nullable=True)
    batch_id = Column(Integer, ForeignKey("run_batches.id"), nullable=True, index=True)
    status = Column(String, nullable=False)
    # 049：失败分类（仅 status=failed 时写入；历史数据为空，前端显示「未知」）
    failure_kind = Column(String(16), nullable=True)
    failure_reason = Column(Text, nullable=True)
    # nullable: 预创建 pending 行留空，避开 _compute_batch_status 卡死检查误判
    start_time = Column(DateTime(timezone=True), nullable=True)
    end_time = Column(DateTime(timezone=True), nullable=True)
    duration = Column(Float, nullable=True)
    report_path = Column(String, nullable=True)
    log_path = Column(String, nullable=True)
    execution_mode = Column(String, default="server", nullable=False)
    is_init = Column(Boolean, default=False, nullable=False)  # 是否初始化用例
    # 039：数据组执行身份（非参数化执行为 NULL）
    dataset_group = Column(Integer, nullable=True, index=True)
    dataset_row = Column(JSON, nullable=True)


class RunLog(Base):
    __tablename__ = "run_logs"

    id = Column(Integer, primary_key=True, index=True)
    run_id = Column(Integer, ForeignKey("test_runs.id"), nullable=False, index=True)
    step_id = Column(Integer, ForeignKey("test_steps.id", ondelete="SET NULL"), nullable=True, index=True)
    timestamp = Column(DateTime(timezone=True), default=tz_now)
    level = Column(String, nullable=False)
    message = Column(Text, nullable=False)
    screenshot_path = Column(String, nullable=True)
