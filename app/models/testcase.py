# app/models/testcase.py
# 测试用例与测试步骤 ORM 模型
from sqlalchemy import Column, Integer, String, Text, Boolean, DateTime, Float, ForeignKey, JSON
from sqlalchemy.orm import relationship

from app.database import Base
from app.tz import now as tz_now


class TestCase(Base):
    __tablename__ = "test_cases"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False, index=True)
    module_id = Column(Integer, ForeignKey("modules.id"), nullable=True, index=True)
    project_case_number = Column(Integer, nullable=False, default=0, index=True)  # 项目内自增编号
    name = Column(String, nullable=False)
    description = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), default=tz_now)
    updated_at = Column(DateTime(timezone=True), default=tz_now, onupdate=tz_now)

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
    retry_max = Column(Integer, default=0)
    retry_delay = Column(Float, default=1.0)
    assertions = Column(JSON, default=[])
    healed_selector = Column(String(500), nullable=True, default=None)
