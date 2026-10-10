# app/models/chat.py
# 034 智能体对话工作台：对话 / 消息 / 用例变更记录 ORM 模型
#
# 原则（data-model.md）：
#   - 仅 3 张新表；草稿（gen_sessions/gen_test_cases）、用例、批次全部复用既有数据；
#   - 引用（refs）与产物卡片（cards）内嵌 JSONB，不建引用表。
from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey, JSON

from app.database import Base
from app.tz import now as tz_now


class ChatConversation(Base):
    """对话：用户级私有，绑定项目上下文与对话助手 Agent。"""

    __tablename__ = "chat_conversations"

    id = Column(Integer, primary_key=True, autoincrement=True)
    title = Column(String(200), nullable=False, default="新对话")
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=True, index=True)
    # 对话助手 Agent（assistant 类型；默认取活跃「测试助手」）
    assistant_agent_id = Column(Integer, ForeignKey("agent_definitions.id"), nullable=True)
    # active / archived
    status = Column(String(16), nullable=False, default="active")
    # 当前有效引用集：[{type: project|environment|agent|case|batch, id, label}]
    refs = Column(JSON, nullable=True)
    # 历史摘要（FR-005）
    summary = Column(Text, nullable=True)
    summarized_until_message_id = Column(Integer, nullable=True)
    created_at = Column(DateTime(timezone=True), default=tz_now)
    updated_at = Column(DateTime(timezone=True), default=tz_now, onupdate=tz_now)


class ChatMessage(Base):
    """对话消息：文本 + 产物卡片（草稿/对照/运行/证据）。

    卡片状态（任务进度/结果）原地更新；文本内容一经落库不可变（编辑重发 v1 不支持）。
    """

    __tablename__ = "chat_messages"

    id = Column(Integer, primary_key=True, autoincrement=True)
    conversation_id = Column(
        Integer, ForeignKey("chat_conversations.id"), nullable=False, index=True
    )
    role = Column(String(16), nullable=False)  # user | assistant
    content = Column(Text, nullable=True)
    # [{type: draft_set|diff|run|evidence, payload…, status…}]（契约见 contracts C3-C5）
    cards = Column(JSON, nullable=True)
    # 该轮使用的引用（快照，含 label）
    refs = Column(JSON, nullable=True)
    # 该轮执行的动作记录（type/params/result/status；供追溯与审计）
    actions = Column(JSON, nullable=True)
    # 关联长任务 id（生成任务 session_id / 批次 id 字符串），用于状态回填与串行控制
    task_id = Column(String(64), nullable=True, index=True)
    created_at = Column(DateTime(timezone=True), default=tz_now)
    updated_at = Column(DateTime(timezone=True), default=tz_now, onupdate=tz_now)


class CaseRevision(Base):
    """用例变更记录（FR-015）：对话写回（source=chat）与页面编辑（source=page）均留痕。"""

    __tablename__ = "case_revisions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    case_id = Column(Integer, ForeignKey("test_cases.id"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    source = Column(String(16), nullable=False, default="chat")  # chat | page
    conversation_id = Column(
        Integer, ForeignKey("chat_conversations.id"), nullable=True, index=True
    )
    before_snapshot = Column(JSON, nullable=False)
    after_snapshot = Column(JSON, nullable=False)
    summary = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), default=tz_now)
