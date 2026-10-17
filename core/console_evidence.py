"""032 US2：console 错误证据 —— 解析、去重、合并与 MCP 采集适配。

纯函数为主，便于两条执行链路（客户端 AgentBridge / 服务端 AgentRunner）
共用同一口径；所有失败路径静默降级，不阻塞执行、不引入新异常面。
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# 单条证据最大长度（防止超长堆栈撑爆报告）
MAX_ENTRY_CHARS = 300

# 解析时跳过的结构性行（playwright-mcp 输出包装）
_SKIP_PREFIXES = ("###", "total messages", "total:", "console messages", "no console messages")

# "Returning 0 messages for level \"error\"" 这类计数行是元信息不是证据（2026-10-10 实测污染步骤视图；
# 2026-10-16：非零计数行同为空转噪声——条目本身才是证据，favicon 过滤后只剩头行更显突兀）
_COUNT_HEADER_RE = re.compile(r"^returning\s+\d+\s+messages?\b", re.IGNORECASE)


def parse_console_messages(text: str | None) -> list[str]:
    """把 browser_console_messages 输出解析为条目列表（容错，不抛异常）。"""
    out: list[str] = []
    for raw in str(text or "").splitlines():
        s = raw.strip()
        if not s:
            continue
        if s.lower().startswith(_SKIP_PREFIXES):
            continue
        if _COUNT_HEADER_RE.match(s):
            continue
        # 浏览器通用噪声（favicon 404）：不作为证据（2026-10-16 用户反馈报告噪声）
        if "favicon.ico" in s:
            continue
        out.append(s[:MAX_ENTRY_CHARS])
    return out


def dedupe_cap(entries: list[str], max_entries: int) -> tuple[list[str], int]:
    """按出现顺序去重并按上限截断；返回 (保留条目, 省略条数)。"""
    seen: set[str] = set()
    kept: list[str] = []
    for e in entries:
        if not e or e in seen:
            continue
        seen.add(e)
        kept.append(e)
    if max_entries > 0 and len(kept) > max_entries:
        return kept[:max_entries], len(kept) - max_entries
    return kept, 0


def apply_console_evidence(
    logs: list[dict], entries: list[str], max_entries: int = 20,
) -> list[dict]:
    """把 console 条目合并进步骤日志。

    - 有失败步骤：合并进最后一个失败步骤的 message（不新增行，避免污染步骤视图）；
    - 无失败步骤：追加一条运行级汇总行（level=warning）；
    - 空条目：no-op。
    """
    kept, truncated = dedupe_cap([e for e in (entries or []) if e], max_entries)
    if not kept:
        return logs
    summary = "; ".join(kept)
    if truncated:
        summary += f"（其余 {truncated} 条已省略）"
    for row in reversed(logs):
        if str(row.get("level", "")).lower() in ("error", "critical"):
            row["message"] = f"{row.get('message') or ''} | [console] {summary}"
            return logs
    logs.append({
        "step_id": None,
        "level": "warning",
        "message": f"[console] {summary}",
        "screenshot_path": None,
    })
    return logs


async def collect_console_via_mcp(mcp_manager: Any, max_entries: int = 20) -> list[str]:
    """服务端本机 MCP 采集（runner 路径）；任何失败返回 []（静默降级）。"""
    try:
        call = getattr(mcp_manager, "call_tool", None)
        if call is None:
            return []
        res = await call("browser_console_messages", {"level": "error", "all": True})
        if not isinstance(res, dict) or not res.get("success"):
            return []
        kept, _ = dedupe_cap(parse_console_messages(res.get("text")), max_entries)
        return kept
    except Exception:  # noqa: BLE001
        logger.debug("console 证据采集失败（静默降级）", exc_info=True)
        return []
