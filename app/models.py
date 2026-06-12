# app/models.py
from pydantic import BaseModel, Field, field_validator
from typing import List, Optional
from datetime import datetime


# ==================== 认证模型 (T007) ====================

class LoginRequest(BaseModel):
    username: str
    password: str


class LoginResponse(BaseModel):
    id: int
    username: str
    role: str
    must_change_password: bool = False


class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str


class UserCreate(BaseModel):
    username: str = Field(..., min_length=1, max_length=255)
    password: str = Field(..., min_length=8)
    role: str = "tester"


class UserUpdate(BaseModel):
    role: Optional[str] = None
    status: Optional[str] = None


class UserResponse(BaseModel):
    id: int
    username: str
    role: str
    status: str
    created_at: Optional[datetime] = None
    last_login_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class ResetPasswordRequest(BaseModel):
    new_password: str = Field(..., min_length=8)

# ----------------------------
# 基础模型
# ----------------------------

class ProjectBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None
    base_url: Optional[str] = None
    browser: str = Field("chromium", pattern="^(chromium|firefox|webkit)$")
    headless: bool = True

class TestCaseBase(BaseModel):
    project_id: int
    module_id: Optional[int] = None
    name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None
    is_init: bool = False

class TestStepBase(BaseModel):
    case_id: int
    step_order: int = Field(..., gt=0)
    description: str = Field(..., min_length=1, max_length=2000)
    parsed_result: Optional[str] = None

class ModuleBase(BaseModel):
    project_id: int
    name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None
    parent_id: Optional[int] = None

# ----------------------------
# 创建模型（用于POST请求）
# ----------------------------

class ProjectCreate(ProjectBase):
    pass

class ModuleCreate(ModuleBase):
    pass


class ProjectUpdate(ProjectBase):
    pass

class TestStepUpdate(BaseModel):
    id: Optional[int] = None # ID is present for existing steps
    step_order: int = Field(..., gt=0)
    description: str = Field(..., min_length=1, max_length=2000)
    parsed_result: Optional[str] = None

class TestCaseUpdate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None
    module_id: Optional[int] = None
    steps: List[TestStepUpdate] = []
    is_init: Optional[bool] = None

class TestStepCreatePayload(BaseModel):
    step_order: int = Field(..., gt=0)
    description: str = Field(..., min_length=1, max_length=2000)
    parsed_result: Optional[str] = None


class TestCaseCreate(TestCaseBase):
    steps: List[TestStepCreatePayload] = []

class TestStepCreate(TestStepBase):
    pass


# ----------------------------
# 响应模型（用于GET请求）
# ----------------------------

class TestStep(TestStepBase):
    id: int

class Module(ModuleBase):
    id: int
    created_at: datetime

class ModuleUpdate(BaseModel):
    project_id: Optional[int] = None
    name: Optional[str] = Field(default=None, min_length=1, max_length=255)
    description: Optional[str] = None
    parent_id: Optional[int] = None

class TestCase(TestCaseBase):
    id: int
    project_case_number: int = 0
    created_at: datetime
    steps: List[TestStep] = []
    is_init: bool = False

class Project(ProjectBase):
    id: int
    created_at: datetime

class RunBatchCreate(BaseModel):
    project_id: int
    name: str = ""
    total_cases: int = 0


class RunBatch(BaseModel):
    id: int
    project_id: int
    name: str = ""
    status: str = "running"
    total_cases: int = 0
    passed: int = 0
    failed: int = 0
    created_at: Optional[datetime] = None
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class RunBatchUpdate(BaseModel):
    name: str


class TestRun(BaseModel):
    id: int
    case_id: int
    batch_id: Optional[int] = None
    status: str
    start_time: datetime
    end_time: datetime
    duration: float
    report_path: Optional[str] = None
    log_path: Optional[str] = None
    is_init: bool = False

# ----------------------------
# 环境管理模型
# ----------------------------

class EnvironmentBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    base_url: str = Field(..., min_length=1)
    browser: str = Field("chromium", pattern="^(chromium|firefox|webkit)$")
    headless: bool = True
    # 预置 cookie 列表，执行测试时自动注入到浏览器上下文
    cookies: list = Field(default_factory=list)

class EnvironmentCreate(EnvironmentBase):
    pass

class EnvironmentUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=255)
    base_url: Optional[str] = None
    browser: Optional[str] = Field(default=None, pattern="^(chromium|firefox|webkit)$")
    headless: Optional[bool] = None
    cookies: Optional[list] = None

class Environment(EnvironmentBase):
    id: int
    project_id: int
    is_default: bool = False
    created_at: Optional[datetime] = None

    @field_validator("cookies", mode="before")
    @classmethod
    def _coerce_cookies(cls, v):
        # 旧 DB 行的 cookies 列为 NULL，序列化为 [] 避免响应校验失败
        if v is None:
            return []
        return v

    model_config = {"from_attributes": True}

class ScheduleBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    cron_expression: str = Field(..., min_length=1)
    task_type: str = Field(..., pattern="^(testcase|module|project)$")
    target_id: int
    description: Optional[str] = ""
    enabled: bool = True

class ScheduleCreate(ScheduleBase):
    pass

class ScheduleUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=255)
    cron_expression: Optional[str] = None
    task_type: Optional[str] = Field(default=None, pattern="^(testcase|module|project)$")
    target_id: Optional[int] = None
    description: Optional[str] = None
    enabled: Optional[bool] = None

class Schedule(ScheduleBase):
    id: int
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    last_run_at: Optional[datetime] = None
    next_run_at: Optional[datetime] = None
    run_count: int = 0

    model_config = {"from_attributes": True}

class TestCasePage(BaseModel):
    items: List[TestCase]
    total_items: int
    page: int
    size: int


class AgentCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    endpoint: str = Field(..., min_length=1, max_length=500)
    description: str = ""


class AgentUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=255)
    endpoint: Optional[str] = None
    description: Optional[str] = None


class Agent(BaseModel):
    id: int
    name: str
    endpoint: str
    description: str = ""
    status: str = "offline"
    last_heartbeat: Optional[datetime] = None
    created_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class AgentLog(BaseModel):
    id: int
    agent_id: int
    level: str = "info"
    message: str
    created_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class AgentLogPage(BaseModel):
    items: List[AgentLog]
    total: int
    page: int
    size: int


class AuditLogResponse(BaseModel):
    id: int
    user_id: int | None = None
    username: str | None = None
    action: str
    details: str | None = None
    ip_address: str | None = None
    created_at: datetime | None = None

    model_config = {"from_attributes": True}


class AuditLogPage(BaseModel):
    items: list[AuditLogResponse]
    total: int
    page: int
    size: int

