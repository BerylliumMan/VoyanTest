# core/vision_frame.py
# 037：视觉帧压缩工具（探索链 / 执行链共用）。
#
# 统一口径（与探索链原实现一致）：PIL 转 RGB → 超宽缩放（LANCZOS）→ JPEG（默认 q72）→
# data URL；PIL 不可用时原样 base64 兜底（保持可用性，不因缺依赖中断执行）。
#
# 两个入口：
# - frame_to_data_url(path)：截图文件 → data URL（探索链沿用）；
# - b64_to_frame_data_url(b64)：截图 base64（客户端回传/服务端临时截图）→ data URL
#   （执行链每轮决策帧用；接受裸 base64 或已带 data: 前缀的字符串）。
from __future__ import annotations

import base64
import io
import logging
from typing import Optional

logger = logging.getLogger(__name__)


def _compress(raw: bytes, max_width: int, quality: int) -> Optional[str]:
    if not raw:
        return None
    try:
        from PIL import Image

        im = Image.open(io.BytesIO(raw)).convert("RGB")
        if im.width > max_width:
            im = im.resize((max_width, int(im.height * max_width / im.width)), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=quality)
        return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
    except Exception:
        mime = "image/png" if raw[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"
        return f"data:{mime};base64," + base64.b64encode(raw).decode()


def frame_to_data_url(path: str, max_width: int = 1152, quality: int = 72) -> Optional[str]:
    """截图文件 → data URL（JPEG 压缩降 token；PIL 不可用时原样 base64 兜底）。"""
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except Exception:
        return None
    return _compress(raw, int(max_width or 1152), int(quality or 72))


def b64_to_frame_data_url(b64: str, max_width: int = 1152, quality: int = 72) -> Optional[str]:
    """截图 base64（裸值或 data: 前缀）→ 压缩 data URL；无效输入返回 None。"""
    s = str(b64 or "").strip()
    if not s:
        return None
    if s.startswith("data:"):
        try:
            s = s.split(",", 1)[1]
        except IndexError:
            return None
    try:
        raw = base64.b64decode(s, validate=False)
    except Exception:
        return None
    return _compress(raw, int(max_width or 1152), int(quality or 72))


__all__ = ["frame_to_data_url", "b64_to_frame_data_url"]
