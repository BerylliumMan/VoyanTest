# app/db_models.py - SQLAlchemy ORM Models for SQLite
from sqlalchemy import Column, Integer, String, Text, Boolean, DateTime, Float, ForeignKey, JSON
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship
from app.database import Base
from app.tz import now as tz_now

class Project(Base):
    __tablename__ = "projects"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True, nullable=False)
    description = Column(Text, nullable=True)
    base_url = Column(String, nullable=True)
    browser = Column(String, default="chromium")
    headless = Column(Boolean, default=True)
    created_at = Column(DateTime, default=tz_now)

    test_cases = relationship("TestCase", backref="project", cascade="save-update, merge")
    environments = relationship("Environment", backref="project", cascade="all, delete-orphan")


class Environment(Base):
    __tablename__ = "environments"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False, index=True)
    name = Column(String, nullable=False)
    base_url = Column(String, nullable=False)
    browser = Column(String, default="chromium")
    headless = Column(Boolean, default=True)
    is_default = Column(Boolean, default=False)
    # 预置 cookie 列表（执行前注入到浏览器上下文，避免重复登录）
    # 结构: [{"name": "session", "value": "xxx", "domain": "example.com", "path": "/"}]
    cookies = Column(JSON, default=list)
    created_at = Column(DateTime, default=tz_now)

class Module(Base):
    __tablename__ = "modules"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False, index=True)
    name = Column(String, nullable=False)
    description = Column(Text, nullable=True)
    parent_id = Column(Integer, ForeignKey("modules.id"), nullable=True, index=True)
    created_at = Column(DateTime, default=tz_now)

    test_cases = relationship("TestCase", backref="module", passive_deletes=True)
    children = relationship("Module", backref="parent", remote_side=[id])

class TestCase(Base):
    __tablename__ = "test_cases"
    
    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False, index=True)
    module_id = Column(Integer, ForeignKey("modules.id"), nullable=True, index=True)
    project_case_number = Column(Integer, nullable=False, default=0, index=True)  # 项目内自增编号
    name = Column(String, nullable=False)
    description = Column(Text, nullable=True)
    created_at = Column(DateTime, default=tz_now)
    updated_at = Column(DateTime, default=tz_now, onupdate=tz_now)
    
    # 版本控制字段
    version = Column(Integer, default=1, nullable=False)
    parent_id = Column(Integer, ForeignKey("test_cases.id"), nullable=True, index=True)
    is_template = Column(Boolean, default=False)
    tags = Column(String, nullable=True)  # 逗号分隔的标签
    priority = Column(String, default="medium")  # low, medium, high, critical
    status = Column(String, default="active")  # active, deprecated, draft
    is_init = Column(Boolean, default=False, nullable=False)  # 是否作为初始化用例

    steps = relationship("TestStep", backref="test_case", lazy="selectin", cascade="all, delete-orphan", order_by="TestStep.step_order")

class TestStep(Base):
    __tablename__ = "test_steps"
    
    id = Column(Integer, primary_key=True, index=True)
    case_id = Column(Integer, ForeignKey("test_cases.id"), nullable=False, index=True)
    step_order = Column(Integer, nullable=False)
    description = Column(String, nullable=False)
    parsed_result = Column(Text, nullable=True)

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
    created_at = Column(DateTime, default=tz_now)
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)

    runs = relationship("TestRun", backref="batch")


class TestRun(Base):
    __tablename__ = "test_runs"

    id = Column(Integer, primary_key=True, index=True)
    case_id = Column(Integer, ForeignKey("test_cases.id"), nullable=False, index=True)
    batch_id = Column(Integer, ForeignKey("run_batches.id"), nullable=True, index=True)
    status = Column(String, nullable=False)
    # nullable: 预创建 pending 行留空，避开 _compute_batch_status 卡死检查误判
    start_time = Column(DateTime, nullable=True)
    end_time = Column(DateTime, nullable=True)
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
    timestamp = Column(DateTime, default=tz_now)
    level = Column(String, nullable=False)
    message = Column(Text, nullable=False)
    screenshot_path = Column(String, nullable=True)

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


# ==================== 认证与用户管理 (003-user-auth) T006 ====================

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(255), unique=True, nullable=False, index=True)
    password_hash = Column(String(255), nullable=False)
    role = Column(String(50), nullable=False, default="tester")
    status = Column(String(50), nullable=False, default="active")
    locked_until = Column(DateTime, nullable=True)
    must_change_password = Column(Boolean, default=True)
    login_attempts = Column(Integer, default=0)
    created_at = Column(DateTime, default=tz_now)
    last_login_at = Column(DateTime, nullable=True)


class Session(Base):
    __tablename__ = "sessions"
    id = Column(String(64), primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    created_at = Column(DateTime, default=tz_now)
    expires_at = Column(DateTime, nullable=False)
    last_activity = Column(DateTime, default=tz_now)


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    action = Column(String(100), nullable=False)
    details = Column(Text, nullable=True)
    ip_address = Column(String(45), nullable=True)
    created_at = Column(DateTime, default=tz_now)


class Agent(Base):
    __tablename__ = "agents"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(255), nullable=False, unique=True)
    endpoint = Column(String(500), nullable=False)
    description = Column(Text, default="")
    status = Column(String(50), default="offline")
    last_heartbeat = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=tz_now)


class AgentLog(Base):
    __tablename__ = "agent_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    agent_id = Column(Integer, ForeignKey("agents.id"), nullable=False, index=True)
    level = Column(String(50), default="info")
    message = Column(Text, nullable=False)
    created_at = Column(DateTime, default=tz_now)


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
    updated_at = Column(DateTime, default=tz_now, onupdate=tz_now)


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
    updated_at = Column(DateTime, default=tz_now, onupdate=tz_now)


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
