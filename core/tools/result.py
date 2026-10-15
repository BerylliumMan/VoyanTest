"""035 工具层：结果契约（ToolResult + shape_result）。

契约 C1：`{ok, summary, data, truncated, next_cursor, error}`；
`data` 序列化超过 chat_tool_result_max_bytes → 分页切片 + 显式截断标注（禁止静默截断，SC-008）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Optional

DEFAULT_MAX_BYTES = 16384
TRUNCATION_GUIDANCE = "结果已截断（truncated=true），使用 limit/offset 翻页或收窄过滤条件"


@dataclass
class ToolResult:
    ok: bool = True
    summary: str = ""
    data: Any = None
    truncated: bool = False
    next_cursor: Optional[int] = None
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "summary": self.summary,
            "data": self.data,
            "truncated": self.truncated,
            "next_cursor": self.next_cursor,
            "error": self.error,
        }


def result_bytes(obj: Any) -> int:
    """JSON 序列化字节数（失败视为超限）。"""
    try:
        return len(json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8"))
    except (TypeError, ValueError):
        return DEFAULT_MAX_BYTES + 1


def shape_result(data: Any, *, summary: str = "", max_bytes: Optional[int] = None,
                 next_cursor: Optional[int] = None) -> ToolResult:
    """统一结果塑形：有界输出 + 分页切片 + 显式截断标注。

    - 预算内：原样返回；
    - dict 含 items 列表超限：渐进切片（尽量多保留条目），truncated=True，
      next_cursor = offset + len(kept)（调用方以 offset 回传翻页）；
    - 其他形态超限：丢弃 data（保留 summary），truncated=True。
    """
    limit = max_bytes or DEFAULT_MAX_BYTES
    if data is None:
        return ToolResult(ok=True, summary=summary or "空结果", data=None, next_cursor=next_cursor)
    if result_bytes(data) <= limit:
        return ToolResult(ok=True, summary=summary, data=data, next_cursor=next_cursor)

    if isinstance(data, dict) and isinstance(data.get("items"), list):
        items = data["items"]
        base = {k: v for k, v in data.items() if k != "items"}
        kept: list = []
        for item in items:
            kept.append(item)
            if result_bytes(dict(base, items=kept, truncated=True)) > limit:
                kept.pop()
                break
        offset = int(data.get("offset") or 0)
        out = dict(base, items=kept, truncated=True)
        return ToolResult(ok=True, summary=summary, data=out, truncated=True,
                          next_cursor=offset + len(kept))

    return ToolResult(ok=True, summary=summary or "结果过大已截断", data=None,
                      truncated=True, next_cursor=next_cursor)


def error_result(message: str, *, summary: str = "") -> ToolResult:
    return ToolResult(ok=False, summary=summary or message, error=message)
