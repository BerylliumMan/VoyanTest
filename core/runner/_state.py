# core/runner/_state.py
"""共享状态与错误分类辅助函数，跨 runner 模块共享。

本模块只负责：
    1. 错误分类（自愈选择器触发判断）— 纯函数，无副作用
    2. （保留扩展点）其他模块级共享辅助

刻意不在这里做：
    - 暂停 / 决策 字典来自 app.websocket，使用方直接 import，避免循环依赖
    - 跨模块状态（如 pause events）应在使用方模块内 import app.websocket
"""
import logging

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
