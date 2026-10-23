# app/models/ai.py
# 040 接口测试 AI：分析结果持久缓存（失败归因 / 契约检测 / 策略报告）
from sqlalchemy import (
    Column, DateTime, ForeignKey, Integer, JSON, String, UniqueConstraint,
)

from app.database import Base
from app.tz import now as tz_now


class ApiAiAnalysis(Base):
    """AI 分析结果缓存。

    - failure / contract：按 (run_id, kind) 唯一；
    - strategy：run_id 为空，按 (project_id, kind, input_hash) 唯一（CRUD 层 upsert）。
    """

    __tablename__ = "api_ai_analyses"
    __table_args__ = (
        UniqueConstraint("run_id", "kind", name="uq_api_ai_analysis_run_kind"),
        UniqueConstraint(
            "project_id", "kind", "input_hash",
            name="uq_api_ai_analysis_project_kind_hash",
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False, index=True)
    run_id = Column(Integer, ForeignKey("test_runs.id"), nullable=True, index=True)
    case_id = Column(Integer, ForeignKey("test_cases.id"), nullable=True, index=True)
    kind = Column(String(16), nullable=False, index=True)  # failure / contract / strategy
    status = Column(String(16), nullable=False, default="ready")  # ready / failed
    input_hash = Column(String(64), nullable=True)
    result = Column(JSON, nullable=True)
    model = Column(String(100), nullable=True)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    stale_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=tz_now)
    updated_at = Column(DateTime(timezone=True), default=tz_now, onupdate=tz_now)
