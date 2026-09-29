# app/models/api_test.py
# 接口测试 ORM 模型：接口定义 / 导入记录 / 数据集 / 场景（029-api-testing）
#
# 接口「用例」不在此文件：它复用 test_cases（case_kind='api' + api_spec JSONB），
# 从而直接复用批次/报告/套件/权限体系（详见 specs/029-api-testing/data-model.md §0）。
from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)

from app.database import Base
from app.tz import now as tz_now


class ApiDefinition(Base):
    """接口定义：来自 OpenAPI/Swagger/Postman 导入或手工创建。"""

    __tablename__ = "api_definitions"
    __table_args__ = (
        UniqueConstraint(
            "project_id", "method", "path", name="uq_api_definition_project_method_path"
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False, index=True)
    module_id = Column(Integer, ForeignKey("modules.id"), nullable=True, index=True)
    name = Column(String(200), nullable=False)
    method = Column(String(10), nullable=False)
    path = Column(String(500), nullable=False)
    protocol = Column(String(16), default="http", nullable=False)
    summary = Column(Text, nullable=True)
    # OpenAPI operationId / Postman item id（判重辅助，可为空）
    operation_id = Column(String(200), nullable=True)
    # {params:[{name,in,type,required,example,enum,schema}], body:{content_type,schema,example}}
    request_schema = Column(JSON, default=dict, nullable=False)
    # {statuses:{"200":{schema,example}}}
    response_schema = Column(JSON, default=dict, nullable=True)
    source = Column(String(20), default="manual", nullable=False)  # openapi3/swagger2/postman/manual
    source_ref = Column(String(500), nullable=True)
    version = Column(Integer, default=1, nullable=False)
    tags = Column(String(200), nullable=True)
    created_at = Column(DateTime(timezone=True), default=tz_now)
    updated_at = Column(DateTime(timezone=True), default=tz_now, onupdate=tz_now)


class ApiImport(Base):
    """接口文档导入记录（统计 + 失败原因）。"""

    __tablename__ = "api_imports"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False, index=True)
    file_name = Column(String(300), nullable=False)
    source = Column(String(20), nullable=False)  # openapi3/swagger2/postman
    total_operations = Column(Integer, default=0, nullable=False)
    created_count = Column(Integer, default=0, nullable=False)
    updated_count = Column(Integer, default=0, nullable=False)
    skipped_count = Column(Integer, default=0, nullable=False)
    error = Column(Text, nullable=True)
    # 定时从 Swagger URL 同步：空 schedule 表示普通导入记录
    url = Column(String(1000), nullable=True)
    schedule = Column(String(100), nullable=True)
    mode = Column(String(16), default="skip", nullable=False)
    basic_username = Column(String(200), nullable=True)
    basic_password = Column(String(200), nullable=True)
    module_id = Column(Integer, ForeignKey("modules.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), default=tz_now)


class ApiDataset(Base):
    """接口用例数据集（P2：数据驱动）。"""

    __tablename__ = "api_datasets"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False, index=True)
    name = Column(String(200), nullable=False)
    columns = Column(JSON, default=list, nullable=False)  # ["username","password"]
    rows = Column(JSON, default=list, nullable=False)  # [["u1","p1"], ...]
    source = Column(String(16), default="manual", nullable=False)  # manual / csv
    created_at = Column(DateTime(timezone=True), default=tz_now)
    updated_at = Column(DateTime(timezone=True), default=tz_now, onupdate=tz_now)


class ApiScenario(Base):
    """接口测试场景：有序的接口用例集合 + 执行环境 + 场景级变量覆盖。

    执行语义：按 steps 顺序逐条服务端执行（复用批次/报告链路，见 scenarios router）；
    ``variables`` 在执行时作为用例级变量注入，覆盖用例自身同名变量（优先级高于
    用例/数据集/环境变量；仍低于步骤级）。steps 形如::

        [{"case_id": 12, "enabled": True, "order": 1}, ...]
    """
    __tablename__ = "api_scenarios"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False, index=True)
    name = Column(String(200), nullable=False)
    description = Column(Text, nullable=True)
    environment_id = Column(Integer, ForeignKey("environments.id"), nullable=True, index=True)
    variables = Column(JSON, default=list, nullable=False)
    steps = Column(JSON, default=list, nullable=False)
    # 默认关闭，避免改变旧场景：整段共用 Cookie；失败后继续后续步骤
    share_cookie = Column(Boolean, default=False, nullable=False)
    continue_on_failure = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime(timezone=True), default=tz_now)
    updated_at = Column(DateTime(timezone=True), default=tz_now, onupdate=tz_now)


class ApiMock(Base):
    """按接口定义返回固定响应，供联调调用。不写测试报告。"""

    __tablename__ = "api_mocks"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False, index=True)
    definition_id = Column(Integer, ForeignKey("api_definitions.id"), nullable=False, index=True)
    name = Column(String(200), nullable=False)
    enabled = Column(Boolean, default=True, nullable=False)
    status_code = Column(Integer, default=200, nullable=False)
    headers = Column(JSON, default=list, nullable=False)
    body = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), default=tz_now)
    updated_at = Column(DateTime(timezone=True), default=tz_now, onupdate=tz_now)


class ApiTestFile(Base):
    """测试文件（031 US1）：平台托管的 multipart 夹具，供接口用例 files[].path 引用。

    引用格式（api_spec 契约）：``platform://<file_id>``；文件落服务端专用目录（卷持久化）。
    上限读取 app.config.Settings.api_test_file_max_mb（默认 50）。
    """

    __tablename__ = "api_test_files"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False)            # 原始文件名（multipart filename 一致）
    size = Column(BigInteger, nullable=False)             # 字节数（上传时校验上限）
    content_type = Column(String(150), nullable=True)     # 推断存储，执行时可覆盖
    storage_path = Column(Text, nullable=False)           # 相对专用目录的存储路径（不进 git）
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=True, index=True)
    uploaded_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), default=tz_now)


class ApiRequestHistory(Base):
    """调试请求历史（031 US7）：服务端持久化，按用户/项目隔离，写后修剪（保留上限在 CRUD 层）。"""

    __tablename__ = "api_request_history"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=True, index=True)
    method = Column(String(16), nullable=False)
    url = Column(Text, nullable=False)                    # 模板态（未渲染变量）
    headers_masked = Column(JSON, default=dict, nullable=True)
    body_preview = Column(Text, nullable=True)            # ≤2KB，已脱敏
    status_code = Column(Integer, nullable=True)          # 传输失败为 NULL
    duration_ms = Column(Integer, nullable=True)
    error = Column(Text, nullable=True)                   # 传输层错误摘要
    created_at = Column(DateTime(timezone=True), default=tz_now, index=True)


class ApiToken(Base):
    """CI 触发令牌（031 US8）：只存 SHA-256 哈希与展示前缀；明文仅在创建响应返回一次。"""

    __tablename__ = "api_tokens"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), nullable=False)
    token_hash = Column(String(128), nullable=False, unique=True, index=True)
    token_prefix = Column(String(16), nullable=False)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=True, index=True)  # NULL=全部可见项目
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    expires_at = Column(DateTime(timezone=True), nullable=True)
    revoked_at = Column(DateTime(timezone=True), nullable=True)
    last_used_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=tz_now)
