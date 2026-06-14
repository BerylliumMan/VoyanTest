# app/models/gen.py
# AI 生成会话、定时任务及其相关 ORM 模型
from sqlalchemy import Column, Integer, String, Text, Boolean, DateTime, Float, ForeignKey
from sqlalchemy.orm import relationship

from app.database import Base
from app.tz import now as tz_now


class GenSession(Base):
    """AI 分析会话记录 — 持久化存储分析历史"""
    __tablename__ = "gen_sessions"

    id = Column(String(64), primary_key=True, index=True)
    filename = Column(String(500), nullable=False)
    filenames = Column(Text, nullable=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=True, index=True)
    project_description = Column(Text, nullable=True)
    status = Column(String(50), nullable=False, default="pending")
    error_message = Column(Text, nullable=True)
    functional_points_count = Column(Integer, default=0)
    test_cases_count = Column(Integer, default=0)
    imported_count = Column(Integer, default=0)
    created_at = Column(DateTime, default=tz_now)
    completed_at = Column(DateTime, nullable=True)

    project = relationship("Project", backref="gen_sessions")
    functional_points = relationship("GenFunctionalPoint", backref="session", cascade="all, delete-orphan")
    test_cases = relationship("GenTestCase", backref="session", cascade="all, delete-orphan")


class GenFunctionalPoint(Base):
    __tablename__ = "gen_functional_points"

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(String(64), ForeignKey("gen_sessions.id"), nullable=False, index=True)
    fp_id = Column(Integer, nullable=False)
    module = Column(String(255), nullable=True)
    name = Column(String(500), nullable=False)
    description = Column(Text, nullable=True)
    category = Column(String(100), nullable=True)


class GenTestCase(Base):
    __tablename__ = "gen_test_cases"

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(String(64), ForeignKey("gen_sessions.id"), nullable=False, index=True)
    test_case_id = Column(String(50), nullable=False)
    module = Column(String(255), nullable=True)
    title = Column(String(500), nullable=False)
    preconditions = Column(Text, nullable=True)
    test_steps = Column(Text, nullable=True)
    expected_result = Column(Text, nullable=True)
    priority = Column(String(20), nullable=True)


# ----------------------------
# 定时任务调度
# ----------------------------

class ScheduledTask(Base):
    """定时任务"""
    __tablename__ = "scheduled_tasks"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(255), nullable=False)
    description = Column(Text, default="")
    cron_expression = Column(String(100), nullable=False)
    task_type = Column(String(50), nullable=False)  # testcase, module, project
    target_id = Column(Integer, nullable=False)
    enabled = Column(Boolean, default=True)
    created_at = Column(DateTime, default=tz_now)
    updated_at = Column(DateTime, default=tz_now, onupdate=tz_now)
    last_run_at = Column(DateTime, nullable=True)
    next_run_at = Column(DateTime, nullable=True)
    run_count = Column(Integer, default=0)


class ScheduledTaskRun(Base):
    """定时任务执行记录"""
    __tablename__ = "scheduled_task_runs"

    id = Column(Integer, primary_key=True, index=True)
    task_id = Column(Integer, ForeignKey("scheduled_tasks.id"), nullable=False, index=True)
    run_id = Column(Integer, ForeignKey("test_runs.id"), nullable=True, index=True)
    status = Column(String, nullable=False)  # success, failed, running
    start_time = Column(DateTime, nullable=False)
    end_time = Column(DateTime, nullable=True)
    duration = Column(Float, nullable=True)
    error_message = Column(Text, nullable=True)
