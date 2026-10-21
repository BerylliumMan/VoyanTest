"""038 US4：显示/导出层的密码打码（不改数据库与 API 原文）。

识别「密码/口令/password/passwd/pwd + 分隔符 + 值」，把值替换为 ******。
只用于消息渲染（前端另有同规则实现）与 Markdown 导出、工具循环回复落库。
"""
from __future__ import annotations

import re

_PASSWORD_RE = re.compile(
    r"(密码|口令|password|passwd|pwd)(\s*[:：=]\s*|\s+)([^\s，,。;；、\"'）)】]+)",
    re.I,
)


def mask_text(text: str | None) -> str:
    if not text:
        return text or ""
    return _PASSWORD_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}******", str(text))
