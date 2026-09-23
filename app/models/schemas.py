# app/models/schemas.py
# Pydantic request/response schemas (moved verbatim from the former app/models.py
# to allow app/models/ to exist as a package alongside the SQLAlchemy ORM models).
from pydantic import BaseModel, Field, field_validator
from typing import Any, List, Optional
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
    project_ids: Optional[List[int]] = None


class UserUpdate(BaseModel):
    role: Optional[str] = None
    status: Optional[str] = None
    project_ids: Optional[List[int]] = None


class UserResponse(BaseModel):
    id: int
    username: str
    role: str
    status: str
    project_ids: Optional[List[int]] = None
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
    tags: Optional[str] = None
    priority: str = "medium"
    case_kind: str = Field("functional", pattern="^(functional|ui|api)$")
    # 接口用例（case_kind='api'）的请求快照；UI/功能用例为 None
    api_spec: Optional[dict] = None
    api_definition_id: Optional[int] = None
    definition_version: Optional[int] = None

class TestStepBase(BaseModel):
    case_id: int
    step_order: int = Field(..., gt=0)
    description: str = Field(..., min_length=1, max_length=2000)
    parsed_result: Optional[str] = None
    retry_max: int = Field(default=0, ge=0)
    retry_delay: float = Field(default=1.0, ge=0.0)
    assertions: list[dict] = Field(default_factory=list)
    healed_selector: Optional[str] = None
    learned_locator: Optional[dict] = None
    # UI StructuredStep dict; optional for functional / legacy NL steps
    structured_step: Optional[dict] = None
    # Midscene-style: when False, never read/write locator memory for this step
    cacheable: bool = True

    @field_validator("retry_max", mode="before")
    @classmethod
    def _coerce_retry_max(cls, v):
        return 0 if v is None else v

    @field_validator("retry_delay", mode="before")
    @classmethod
    def _coerce_retry_delay(cls, v):
        return 1.0 if v is None else v

    @field_validator("assertions", mode="before")
    @classmethod
    def _coerce_assertions(cls, v):
        return [] if v is None else v

    @field_validator("cacheable", mode="before")
    @classmethod
    def _coerce_cacheable(cls, v):
        return True if v is None else v

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
    # 建模块以 URL 路径的项目为准（router 在 body 提供时会交叉校验一致性），故可省略
    project_id: Optional[int] = None


class ProjectUpdate(ProjectBase):
    pass

class TestStepUpdate(BaseModel):
    id: Optional[int] = None
    step_order: int = Field(..., gt=0)
    description: str = Field(..., min_length=1, max_length=2000)
    parsed_result: Optional[str] = None
    retry_max: Optional[int] = None
    retry_delay: Optional[float] = None
    assertions: Optional[list[dict]] = None
    healed_selector: Optional[str] = None
    learned_locator: Optional[dict] = None
    # UI StructuredStep dict; optional for functional / legacy NL steps
    structured_step: Optional[dict] = None
    # Midscene-style: when False, never read/write locator memory for this step
    cacheable: bool = True

class TestCaseUpdate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None
    module_id: Optional[int] = None
    steps: List[TestStepUpdate] = []
    is_init: Optional[bool] = None
    tags: Optional[str] = None
    priority: Optional[str] = None
    case_kind: Optional[str] = Field(None, pattern="^(functional|ui|api)$")
    api_spec: Optional[dict] = None

class TestStepCreatePayload(BaseModel):
    step_order: int = Field(..., gt=0)
    description: str = Field(..., min_length=1, max_length=2000)
    parsed_result: Optional[str] = None
    retry_max: int = Field(default=0, ge=0)
    retry_delay: float = Field(default=1.0, ge=0.0)
    assertions: list[dict] = Field(default_factory=list)
    structured_step: Optional[dict] = None
    learned_locator: Optional[dict] = None
    cacheable: bool = True


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
    case_kind: str = "functional"
    api_spec: Optional[dict] = None
    compiled_script: Optional[str] = None
    compiled_script_hash: Optional[str] = None
    compiled_at: Optional[datetime] = None

    model_config = {"from_attributes": True}

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
    # 接口测试环境变量 [{key,value,secret,enable}] 与公共请求头 [{key,value,enable}]
    # 类型用 Any：结构校验（非 list / 元素缺 key）在 router 层做，返回 400 可读错误
    variables: Any = Field(default_factory=list)
    headers: Any = Field(default_factory=list)

class EnvironmentCreate(EnvironmentBase):
    pass

class EnvironmentUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=255)
    base_url: Optional[str] = None
    browser: Optional[str] = Field(default=None, pattern="^(chromium|firefox|webkit)$")
    headless: Optional[bool] = None
    cookies: Optional[list] = None
    variables: Optional[Any] = None
    headers: Optional[Any] = None

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

    @field_validator("variables", mode="before")
    @classmethod
    def _mask_variables(cls, v):
        """GET 回显脱敏（契约 §1.6）：secret=true 的变量值打码为 "******"，
        附 has_value 布尔（有值 true / 空值 false）；非 secret 原样返回。"""
        if v is None:
            return []
        out = []
        for it in v:
            if not isinstance(it, dict):
                out.append(it)
                continue
            item = dict(it)
            if item.get("secret"):
                has_value = item.get("value") not in (None, "")
                item["value"] = "******"
                item["has_value"] = has_value
            out.append(item)
        return out

    model_config = {"from_attributes": True}

class ScheduleBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    cron_expression: str = Field(..., min_length=1)
    task_type: str = Field(..., pattern="^(testcase|module|project|api_scenario|api_import)$")
    target_id: int
    description: Optional[str] = ""
    enabled: bool = True

class ScheduleCreate(ScheduleBase):
    pass

class ScheduleUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=255)
    cron_expression: Optional[str] = None
    task_type: Optional[str] = Field(default=None, pattern="^(testcase|module|project|api_scenario|api_import)$")
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


# ==================== AgentDefinition 模型 (021-agent-definition) ====================

class AgentDefinitionCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    agent_type: str = Field(..., pattern=r"^(generation|execution|recording)$")
    description: str = ""
    skills: list[str] = Field(default_factory=list)
    llm_config: dict = Field(default_factory=dict)
    prompt_overrides: dict[str, str] = Field(default_factory=dict)
    system_prompt: str = ""
    tools: list[dict] = Field(default_factory=list)
    goal: str = ""
    constraints: list[dict] = Field(default_factory=list)
    thinking_config: dict = Field(default_factory=dict)
    is_active: bool = False


class AgentDefinitionUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=100)
    agent_type: Optional[str] = Field(default=None, pattern=r"^(generation|execution|recording)$")
    description: Optional[str] = None
    skills: Optional[list[str]] = None
    llm_config: Optional[dict] = None
    prompt_overrides: Optional[dict[str, str]] = None
    system_prompt: Optional[str] = None
    tools: Optional[list[dict]] = None
    goal: Optional[str] = None
    constraints: Optional[list[dict]] = None
    thinking_config: Optional[dict] = None
    is_active: Optional[bool] = None


class AgentDefinitionResponse(BaseModel):
    id: int
    name: str
    agent_type: str
    description: str
    skills: list[str]
    llm_config: dict
    prompt_overrides: dict
    system_prompt: str = ""
    tools: list[dict] = Field(default_factory=list)
    goal: str = ""
    constraints: list[dict] = Field(default_factory=list)
    thinking_config: dict = Field(default_factory=dict)
    is_active: bool
    created_at: Optional[datetime] = None

    @field_validator("system_prompt", mode="before")
    @classmethod
    def coerce_none_to_empty(cls, v: str | None) -> str:
        return v if v is not None else ""

    model_config = {"from_attributes": True}


# ==================== Agent 运行模型 (023-true-agent) ====================

class AgentRunCreate(BaseModel):
    agent_definition_id: int
    case_id: Optional[int] = None
    goal: dict = Field(default_factory=dict)


class AgentRunResponse(BaseModel):
    id: int
    agent_definition_id: int
    agent_definition_name: str = ""
    case_id: Optional[int] = None
    status: str = "pending"
    goal: dict = Field(default_factory=dict)
    result: Optional[Any] = None
    turns_used: int = 0
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    duration_ms: Optional[int] = None
    error: Optional[str] = None
    created_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class AgentMessageResponse(BaseModel):
    id: int
    run_id: int
    turn_number: int
    role: str
    content: str
    tool_calls: Optional[list] = None
    token_count: int = 0
    created_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


# ----------------------------
# 测试用例集
# ----------------------------

class TestSuiteCaseItem(BaseModel):
    case_id: int
    order_index: int = 0
    name: Optional[str] = None
    module_id: Optional[int] = None


class TestSuiteCreate(BaseModel):
    project_id: int
    name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None
    case_kind: str = Field("ui", pattern="^(functional|ui|api)$")
    case_ids: List[int] = []


class TestSuiteUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    description: Optional[str] = None
    case_ids: Optional[List[int]] = None  # 有序完整列表；None 表示不改用例


class TestSuite(BaseModel):
    id: int
    project_id: int
    name: str
    description: Optional[str] = None
    case_kind: str = "ui"
    case_count: int = 0
    cases: List[TestSuiteCaseItem] = []
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class SuiteRunRequest(BaseModel):
    environment_id: Optional[int] = None
    init_case_ids: List[int] = []
    agent_name: Optional[str] = None
    backend: Optional[str] = None


# ==================== 接口测试（029-api-testing） ====================
# 契约：specs/029-api-testing/contracts/api-test-contract.md；结构：data-model.md §3

class ApiKeyValue(BaseModel):
    """通用键值项（请求头/查询参数/变量/用例变量）。"""

    key: str
    value: str = ""
    enable: bool = True
    secret: bool = False


class ApiBodySpec(BaseModel):
    type: str = "none"  # none|json|form|form_data|raw|binary
    content: str = ""


class ApiAuthSpec(BaseModel):
    type: str = "none"  # none|basic|bearer|api_key
    username: Optional[str] = None
    password: Optional[str] = None
    token: Optional[str] = None
    key_name: Optional[str] = None
    key_value: Optional[str] = None
    in_: Optional[str] = None  # api_key 位置：header|query


class ApiRequestSpec(BaseModel):
    method: str = "GET"
    url: str = ""
    headers: List[ApiKeyValue] = []
    query: List[ApiKeyValue] = []
    body: ApiBodySpec = ApiBodySpec()
    auth: ApiAuthSpec = ApiAuthSpec()
    timeout_ms: int = Field(default=30000, ge=1000, le=300000)
    follow_redirects: bool = True
    verify_ssl: bool = True


class ApiAssertionSpec(BaseModel):
    enable: bool = True
    type: str  # status_code|jsonpath|header|body_contains|body_regex|response_time|jsonschema
    condition: str = "equals"
    expression: str = ""
    expected: str = ""
    name: str = ""


class ApiExtractorSpec(BaseModel):
    enable: bool = True
    type: str  # jsonpath|regex|header|cookie
    expression: str
    variable: str
    scope: str = "case"  # case|environment
    required: bool = True


class ApiStepSpec(BaseModel):
    order: int = Field(ge=1)
    name: str = ""
    enable: bool = True
    definition_id: Optional[int] = None
    request: ApiRequestSpec
    assertions: List[ApiAssertionSpec] = []
    extractors: List[ApiExtractorSpec] = []
    pre: List[dict] = []
    post: List[dict] = []


class ApiSpecPayload(BaseModel):
    """test_cases.api_spec 的载荷（schema_version=1）。"""

    schema_version: int = 1
    variables: List[ApiKeyValue] = []
    dataset_id: Optional[int] = None
    fail_policy: str = "fail_fast"  # fail_fast|continue
    steps: List[ApiStepSpec] = []


class ApiImportRequest(BaseModel):
    project_id: int
    module_id: Optional[int] = None
    on_conflict: str = "skip"  # skip|overwrite
    swagger_url: Optional[str] = None


class ApiImportResponse(BaseModel):
    import_id: int
    source: str
    total_operations: int
    created: int
    updated: int
    skipped: int
    warnings: List[str] = []
    operations: List[dict] = []


class ApiDefinitionResponse(BaseModel):
    id: int
    project_id: int
    module_id: Optional[int] = None
    name: str
    method: str
    path: str
    summary: Optional[str] = None
    tags: Optional[str] = None
    operation_id: Optional[str] = None
    source: str = "manual"
    request_schema: dict = {}
    response_schema: Optional[dict] = None
    updated_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class ApiDefinitionUpdate(BaseModel):
    name: Optional[str] = None
    summary: Optional[str] = None
    tags: Optional[str] = None
    module_id: Optional[int] = None


class ApiImportHistoryItem(BaseModel):
    id: int
    file_name: str
    source: str
    total_operations: int
    created_count: int
    updated_count: int
    skipped_count: int
    error: Optional[str] = None
    created_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class ApiGenerateOptions(BaseModel):
    normal: bool = True
    boundary: bool = True
    missing_required: bool = True
    type_error: bool = False
    auth_fail: bool = False
    max_cases_per_operation: int = Field(default=5, ge=1, le=20)
    use_llm: bool = True


class ApiGenerateRequest(BaseModel):
    project_id: int
    definition_ids: List[int] = []
    module_id: Optional[int] = None
    options: ApiGenerateOptions = ApiGenerateOptions()


class ApiGenerateCase(BaseModel):
    draft_id: int
    definition_id: int
    name: str
    priority: str = "medium"
    api_spec: dict = {}
    notes: List[str] = []


class ApiGenerateResponse(BaseModel):
    session_id: str
    total: int
    cases: List[ApiGenerateCase] = []


class ApiGenerateImportRequest(BaseModel):
    draft_ids: List[int] = []


class ApiDebugRequest(BaseModel):
    environment_id: Optional[int] = None
    case_variables: List[ApiKeyValue] = []
    step: dict
    dry_run: bool = False


class ApiDebugResponse(BaseModel):
    rendered: dict = {}
    response: Optional[dict] = None
    assertions: List[dict] = []
    extracted: List[dict] = []
    error: Optional[str] = None


class ApiDatasetPayload(BaseModel):
    project_id: int
    name: str
    columns: List[str] = []
    rows: List[dict[str, Any]] = []
    source: str = "manual"


class ApiDatasetResponse(BaseModel):
    id: int
    project_id: int
    name: str
    columns: List[str] = []
    rows: List[dict[str, Any]] = []
    source: str = "manual"
    updated_at: Optional[datetime] = None

    model_config = {"from_attributes": True}
