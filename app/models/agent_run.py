# app/models/agent_run.py
# Agent 运行记录、消息、工具调用、检查点 ORM 模型

from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey, JSON

from app.database import Base
from app.tz import now as tz_now


class AgentRun(Base):
    __tablename__ = "agent_runs"

    id = Column(Integer, primary_key=True, index=True)
    agent_definition_id = Column(Integer, ForeignKey("agent_definitions.id", ondelete="CASCADE"), nullable=False, index=True)
    case_id = Column(Integer, ForeignKey("test_cases.id", ondelete="SET NULL"), nullable=True)
    status = Column(String(50), default="pending")        # pending/running/paused/completed/failed/cancelled
    goal = Column(JSON, default=dict)                     # 目标快照
    result = Column(JSON, nullable=True)                  # 最终结果摘要
    partial_results = Column(JSON, default=list)          # 每个 turn 的 act() 结果 [{turn, tool, success}]
    turns_used = Column(Integer, default=0)
    started_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    duration_ms = Column(Integer, nullable=True)
    error = Column(Text, nullable=True)
    idempotency_key = Column(String(255), unique=True, nullable=True)
    created_at = Column(DateTime(timezone=True), default=tz_now)


class AgentMessage(Base):
    __tablename__ = "agent_messages"

    id = Column(Integer, primary_key=True, index=True)
    run_id = Column(Integer, ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False, index=True)
    turn_number = Column(Integer, nullable=False)
    role = Column(String(20), nullable=False)              # system/user/assistant/tool
    content = Column(Text, nullable=False)
    tool_calls = Column(JSON, nullable=True)
    token_count = Column(Integer, default=0)
    created_at = Column(DateTime(timezone=True), default=tz_now)


class AgentToolCall(Base):
    __tablename__ = "agent_tool_calls"

    id = Column(Integer, primary_key=True, index=True)
    run_id = Column(Integer, ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False, index=True)
    turn_number = Column(Integer, nullable=False)
    tool_name = Column(String(200), nullable=False)
    tool_args = Column(JSON, default=dict)
    tool_result = Column(JSON, nullable=True)
    success = Column(Integer, default=1)                   # 0/1
    error_message = Column(Text, nullable=True)
    duration_ms = Column(Integer, nullable=True)
    created_at = Column(DateTime(timezone=True), default=tz_now)


class AgentRunSnapshot(Base):
    __tablename__ = "agent_run_snapshots"

    id = Column(Integer, primary_key=True, index=True)
    run_id = Column(Integer, ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False, index=True)
    turn_number = Column(Integer, nullable=False)
    context_json = Column(JSON, nullable=False)            # 完整上下文序列化
    compressed_count = Column(Integer, default=0)
    token_count = Column(Integer, default=0)
    created_at = Column(DateTime(timezone=True), default=tz_now)