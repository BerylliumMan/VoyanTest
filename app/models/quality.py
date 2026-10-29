# 042 质量决策与治理：回归计划 / Flaky 状态 / 需求 / 需求关联
from sqlalchemy import (
    JSON, Boolean, Column, DateTime, ForeignKey, Integer, String, Text,
    UniqueConstraint,
)

from app.database import Base
from app.tz import now as tz_now


class QualityRegressionPlan(Base):
    __tablename__ = "quality_regression_plans"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False, index=True)
    name = Column(String(200), nullable=False)
    description = Column(Text, nullable=True)
    environment_id = Column(Integer, ForeignKey("environments.id"), nullable=True)
    agent_id = Column(Integer, ForeignKey("agent_definitions.id"), nullable=True)
    items = Column(JSON, nullable=False, default=list)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), default=tz_now, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=tz_now, onupdate=tz_now, nullable=False)
    last_batch_id = Column(Integer, ForeignKey("run_batches.id"), nullable=True)


class QualityFlakyState(Base):
    __tablename__ = "quality_flaky_states"
    __table_args__ = (UniqueConstraint("case_id", name="uq_quality_flaky_case"),)

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False, index=True)
    case_id = Column(Integer, ForeignKey("test_cases.id", ondelete="CASCADE"), nullable=False, index=True)
    status = Column(String(16), nullable=False, default="candidate", index=True)
    sample_runs = Column(Integer, nullable=False, default=0)
    passed = Column(Integer, nullable=False, default=0)
    failed = Column(Integer, nullable=False, default=0)
    consecutive_passes = Column(Integer, nullable=False, default=0)
    last_failed_at = Column(DateTime(timezone=True), nullable=True)
    last_checked_at = Column(DateTime(timezone=True), nullable=True)
    reason = Column(Text, nullable=True)
    last_batch_id = Column(Integer, ForeignKey("run_batches.id"), nullable=True)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    updated_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), default=tz_now, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=tz_now, onupdate=tz_now, nullable=False)


class QualityRequirement(Base):
    __tablename__ = "quality_requirements"
    __table_args__ = (UniqueConstraint("project_id", "name", name="uq_quality_requirement_project_name"),)

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False, index=True)
    name = Column(String(300), nullable=False)
    module_id = Column(Integer, ForeignKey("modules.id"), nullable=True, index=True)
    source = Column(String(16), nullable=False, default="manual")
    source_session_id = Column(String(64), nullable=True)
    description = Column(Text, nullable=True)
    status = Column(String(16), nullable=False, default="active", index=True)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), default=tz_now, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=tz_now, onupdate=tz_now, nullable=False)


class QualityRequirementLink(Base):
    __tablename__ = "quality_requirement_links"
    __table_args__ = (UniqueConstraint("requirement_id", "case_id", name="uq_quality_requirement_case"),)

    id = Column(Integer, primary_key=True, index=True)
    requirement_id = Column(Integer, ForeignKey("quality_requirements.id", ondelete="CASCADE"), nullable=False, index=True)
    case_id = Column(Integer, ForeignKey("test_cases.id", ondelete="CASCADE"), nullable=False, index=True)
    case_kind = Column(String(16), nullable=False, default="ui")
    source = Column(String(16), nullable=False, default="manual")
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), default=tz_now, nullable=False)
