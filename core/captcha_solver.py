# core/captcha_solver.py
"""图形验证码识别（ddddocr）——对话浏览器探索 / 登录时的自动识别（2026-10-14）。

设计要点：
- 本地识别：图片只来自目标页面本身（data URL），不外发任何第三方服务；
- 依赖可选：ddddocr 未安装时优雅降级（返回可读错误，探索流程不中断）；
- 算术验证码：识别文本形如「1+2=?」时自动计算答案（常见于国内系统登录页）；
- 单例 + 锁：模型加载一次复用；识别串行化（验证码调用低频，无需并发优化）。
"""
from __future__ import annotations

import base64
import logging
import re
import threading

logger = logging.getLogger(__name__)

_ocr = None
_lock = threading.Lock()

_ARITHMETIC_RE = re.compile(r"^\s*(\d{1,3})\s*([+\-×xX*])\s*(\d{1,3})\s*=?\s*\??\s*$")


def is_available() -> bool:
    """ddddocr 是否可用（未安装不报错）。"""
    try:
        import ddddocr  # noqa: F401

        return True
    except Exception:
        return False


def _get_ocr():
    global _ocr
    if _ocr is None:
        with _lock:
            if _ocr is None:
                import ddddocr

                _ocr = ddddocr.DdddOcr(show_ad=False)
    return _ocr


def _decode_data_url(data_url: str) -> bytes:
    raw = str(data_url or "")
    if raw.strip().startswith("data:"):
        raw = raw.split(",", 1)[1] if "," in raw else ""
    return base64.b64decode(raw)


def solve_arithmetic(text: str) -> str | None:
    """识别文本为算术式（如 1+2=?、3×4）时返回计算结果，否则 None。"""
    m = _ARITHMETIC_RE.match(str(text or ""))
    if not m:
        return None
    a, op, b = int(m.group(1)), m.group(2), int(m.group(3))
    if op == "+":
        return str(a + b)
    if op == "-":
        return str(a - b)
    return str(a * b)


def solve_text_captcha(data_url: str) -> tuple[str, str]:
    """识别图形验证码；返回 (填写值, 错误)。

    - 常规验证码：返回识别文本（已去空白/保留数字字母）；
    - 算术验证码：返回计算结果（如「1+2=?」→「3」）。
    """
    try:
        payload = _decode_data_url(data_url)
    except Exception:
        return "", "验证码图片数据无效"
    if not payload:
        return "", "验证码图片为空"
    try:
        ocr = _get_ocr()
    except Exception as exc:
        logger.warning("ddddocr 不可用: %s", exc)
        return "", "验证码识别组件不可用（未安装 ddddocr）"
    try:
        with _lock:
            raw = ocr.classification(payload)
    except Exception as exc:
        logger.warning("验证码识别失败: %s", exc, exc_info=True)
        return "", f"验证码识别失败（{type(exc).__name__}）"
    text = "".join(ch for ch in str(raw or "") if ch.isalnum() or ch in "+-×xX*=?").strip()
    if not text:
        return "", "验证码识别结果为空，可刷新验证码后重试"
    computed = solve_arithmetic(text)
    return (computed if computed is not None else text), ""
