# app/models/api_test.py
# 接口测试 ORM 模型：接口定义 / 导入记录 / 数据集 / 场景（029-api-testing）
#
# 接口「用例」不在此文件：它复用 test_cases（case_kind='api' + api_spec JSONB），
# 从而直接复用批次/报告/套件/权限体系（详见 specs/029-api-testing/data-model.md §0）。
from sqlalchemy import (
    JSON,
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
    created_at = Column(DateTime(timezone=True), default=tz_now)
    updated_at = Column(DateTime(timezone=True), default=tz_now, onupdate=tz_now)
