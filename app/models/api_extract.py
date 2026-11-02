# app/models/api_extract.py
# 046：Java 接口自动提取与变更触发
#   - code_repo_bindings：项目 ↔ Git 仓库绑定（凭据加密存储）
#   - api_sync_records：每次同步的结果
#   - api_change_triggers：变更影响的待确认/已执行计划（1 小时确认窗口）
from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)

from app.database import Base
from app.tz import now as tz_now


class CodeRepoBinding(Base):
    """项目绑定的代码仓库（一个项目一个绑定）。"""

    __tablename__ = "code_repo_bindings"
    __table_args__ = (UniqueConstraint("project_id", name="uq_code_repo_binding_project"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    repo_url = Column(String(500), nullable=False)
    branch = Column(String(200), nullable=False, default="main")
    # https_token | ssh_key
    auth_type = Column(String(16), nullable=False, default="https_token")
    # 加密后的 token 或私钥（不落明文）
    auth_secret = Column(Text, nullable=True)
    # manual | scheduled
    sync_mode = Column(String(16), nullable=False, default="scheduled")
    sync_interval_minutes = Column(Integer, nullable=False, default=30)
    # auto | openapi | spring_source
    extractor = Column(String(32), nullable=False, default="auto")
    # 显式指定 OpenAPI 地址（可空，auto 时探测）
    openapi_url = Column(String(500), nullable=True)
    # 源码根目录（多模块仓库可指定子目录）
    source_root = Column(String(300), nullable=True)
    # 046 v2：项目级确认窗口（分钟）；NULL=继承全局
    confirm_window_minutes = Column(Integer, nullable=True)
    # 046 v2：每 N 次增量同步后做一次全量校准
    full_sync_every = Column(Integer, nullable=False, default=10)
    last_sync_at = Column(DateTime(timezone=True), nullable=True)
    last_commit = Column(String(64), nullable=True)
    last_error = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), default=tz_now, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=tz_now, onupdate=tz_now, nullable=False)


class ApiSyncRecord(Base):
    """一次同步的执行记录。"""

    __tablename__ = "api_sync_records"

    id = Column(Integer, primary_key=True, autoincrement=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    # manual | schedule
    trigger = Column(String(16), nullable=False, default="manual")
    started_at = Column(DateTime(timezone=True), default=tz_now, nullable=False)
    finished_at = Column(DateTime(timezone=True), nullable=True)
    base_commit = Column(String(64), nullable=True)
    head_commit = Column(String(64), nullable=True)
    added = Column(Integer, nullable=False, default=0)
    updated = Column(Integer, nullable=False, default=0)
    removed = Column(Integer, nullable=False, default=0)
    # success | failed | no_change
    status = Column(String(16), nullable=False, default="success")
    error = Column(Text, nullable=True)
    # 技术栈探测与提取统计
    details = Column(JSON, nullable=True, default=dict)
    # 046 v2：手动取消同步
    cancel_requested = Column(Boolean, nullable=False, default=False)
    cancelled_at = Column(DateTime(timezone=True), nullable=True)


class ApiChangeTrigger(Base):
    """代码变更触发的接口回归计划（待确认 → 确认/取消/超时执行）。"""

    __tablename__ = "api_change_triggers"

    id = Column(Integer, primary_key=True, autoincrement=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    from_commit = Column(String(64), nullable=True)
    to_commit = Column(String(64), nullable=True)
    plan_id = Column(Integer, ForeignKey("quality_regression_plans.id", ondelete="SET NULL"), nullable=True)
    # pending | running | done | failed | cancelled | expired_skipped
    status = Column(String(20), nullable=False, default="pending", index=True)
    deadline_at = Column(DateTime(timezone=True), nullable=False, index=True)
    claimed_at = Column(DateTime(timezone=True), nullable=True)
    retry_at = Column(DateTime(timezone=True), nullable=True)
    retry_count = Column(Integer, nullable=False, default=0)
    decided_at = Column(DateTime(timezone=True), nullable=True)
    decided_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    # confirmed | cancelled | auto_ran | expired_skipped | failed
    decision = Column(String(20), nullable=True)
    reason = Column(Text, nullable=True)
    case_count = Column(Integer, nullable=False, default=0)
    batch_id = Column(Integer, ForeignKey("run_batches.id"), nullable=True)
    # [{definition_id, method, path, case_ids, reason}]
    affected = Column(JSON, nullable=False, default=list)
    created_at = Column(DateTime(timezone=True), default=tz_now, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=tz_now, onupdate=tz_now, nullable=False)
