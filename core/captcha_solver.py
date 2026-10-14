# core/captcha_solver.py
"""图形验证码识别（ddddocr）——对话浏览器探索 / 登录时的自动识别（2026-10-14）。

设计要点：
- 本地识别：图片只来自目标页面本身（data URL），不外发任何第三方服务；
- 依赖可选：ddddocr 未安装时优雅降级（返回可读错误，探索流程不中断）；
- 算术验证码：识别文本形如「1+2=?」时自动计算答案（常见于国内系统登录页）；
- 识别增强（2026-10-14 实测加固）：灰度预处理 + 小图放大（ddddocr 对低分辨率不友好）；
  默认模型与 beta 模型双跑；给出期望长度（输入框 maxlength）时优先取长度匹配的结果；
- 单例 + 锁：模型加载一次复用；识别串行化（验证码调用低频，无需并发优化）。
"""
from __future__ import annotations

import base64
import io
import logging
import re
import threading

logger = logging.getLogger(__name__)

_ocr_cache: dict[bool, object] = {}
_lock = threading.Lock()

_ARITHMETIC_RE = re.compile(r"^\s*(\d{1,3})\s*([+\-×xX*])\s*(\d{1,3})\s*=?\s*\??\s*$")


def is_available() -> bool:
    """ddddocr 是否可用（未安装不报错）。"""
    try:
        import ddddocr  # noqa: F401

        return True
    except Exception:
        return False


def _get_ocr(beta: bool = False):
    cached = _ocr_cache.get(beta)
    if cached is not None:
        return cached
    with _lock:
        cached = _ocr_cache.get(beta)
        if cached is None:
            import ddddocr

            cached = ddddocr.DdddOcr(show_ad=False, beta=beta)
            _ocr_cache[beta] = cached
    return cached


def _decode_data_url(data_url: str) -> bytes:
    raw = str(data_url or "")
    if raw.strip().startswith("data:"):
        raw = raw.split(",", 1)[1] if "," in raw else ""
    return base64.b64decode(raw)


def _preprocess(payload: bytes) -> bytes:
    """灰度 + 小图放大：ddddocr 对 ~100px 宽的验证码识别率明显更好。"""
    try:
        from PIL import Image

        im = Image.open(io.BytesIO(payload)).convert("L")
        w, h = im.size
        if w < 240:
            scale = max(2, int(240 / max(w, 1)))
            im = im.resize((w * scale, h * scale), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, format="PNG")
        return buf.getvalue()
    except Exception:
        logger.debug("captcha preprocess failed, use raw payload", exc_info=True)
        return payload


def _clean(raw: str) -> str:
    return "".join(ch for ch in str(raw or "") if ch.isalnum() or ch in "+-×xX*=?").strip()


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


def solve_text_captcha(data_url: str, *, prefer_len: int | None = None) -> tuple[str, str]:
    """识别图形验证码；返回 (填写值, 错误)。

    - 常规验证码：返回识别文本（已去空白/保留数字字母）；
    - 算术验证码：返回计算结果（如「1+2=?」→「3」）；
    - ``prefer_len``（输入框 maxlength）给出时：在「预处理默认模型 / 预处理 beta / 原图默认 / 原图 beta」
      四种组合中优先取长度一致的结果（验证码长度通常等于 maxlength）。
    """
    try:
        payload = _decode_data_url(data_url)
    except Exception:
        return "", "验证码图片数据无效"
    if not payload:
        return "", "验证码图片为空"

    variants: list[tuple[str, bytes, bool]] = [
        ("preproc", _preprocess(payload), False),
        ("preproc-beta", _preprocess(payload), True),
        ("raw", payload, False),
        ("raw-beta", payload, True),
    ]

    first_text = ""
    first_err = ""
    for label, buf, beta in variants:
        try:
            ocr = _get_ocr(beta=beta)
        except Exception as exc:
            logger.warning("ddddocr 不可用: %s", exc)
            return "", "验证码识别组件不可用（未安装 ddddocr）"
        try:
            with _lock:
                raw = ocr.classification(buf)
        except Exception as exc:  # noqa: BLE001
            logger.warning("验证码识别失败(%s): %s", label, exc, exc_info=True)
            first_err = first_err or f"验证码识别失败（{type(exc).__name__}）"
            continue
        text = _clean(raw)
        if not text:
            continue
        if not prefer_len:
            # 无长度约束：首个非空结果直接返回（预处理的默认模型优先级最高）
            computed = solve_arithmetic(text)
            return (computed if computed is not None else text), ""
        if len(text) == prefer_len:
            computed = solve_arithmetic(text)
            return (computed if computed is not None else text), ""
        if not first_text:
            first_text = text

    if first_text:
        computed = solve_arithmetic(first_text)
        return (computed if computed is not None else first_text), ""
    return "", first_err or "验证码识别结果为空，可刷新验证码后重试"
