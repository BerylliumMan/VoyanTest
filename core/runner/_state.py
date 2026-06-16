# core/runner/_state.py
"""全局状态与错误分类辅助函数，跨 runner 模块共享。"""
import logging

from app.websocket import _pause_events, _pause_decisions  # noqa: F401  -- 从 app.websocket 导入，确保所有模块共享同一可变字典

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 自愈错误分类 — 只有定位类错误才触发 AI 自愈选择器
# ---------------------------------------------------------------------------

_HEALABLE_ERROR_PATTERNS = [
    "element not found", "no element", "selector", "locator",
    "waiting for", "timeout exceeded", "could not find", "unable to find",
]


def _is_healable_error(error_msg: str) -> bool:
    """判断错误是否由选择器定位失败引起，应该尝试 AI 自愈。"""
    if not error_msg:
        return False
    error_lower = error_msg.lower()
    return any(p in error_lower for p in _HEALABLE_ERROR_PATTERNS)
