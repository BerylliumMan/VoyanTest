# app/routers/chat/__init__.py
# 034 对话工作台路由组。
from fastapi import APIRouter

from app.routers.chat import actions, conversations, evidence, execute
from app.services import (  # noqa: F401 —— 触发动作处理器注册（generate/revise_drafts/edit_case/run_case/explain/set_refs/browse_generate）
    chat_browse,
    chat_edit,
    chat_evidence,
    chat_gen,
    chat_refs,
    chat_run,
)
from core.tools import builtin  # noqa: F401 —— 035：内置工具注册（导入即注册）

router = APIRouter()
router.include_router(conversations.router)
router.include_router(actions.router)
router.include_router(execute.router)
router.include_router(evidence.router)
