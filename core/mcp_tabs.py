"""Helpers for Playwright MCP browser_tabs list/select.

Playwright MCP ``browser_click`` does not auto-focus popups / target=_blank
tabs. After a click we detect new tabs via ``browser_tabs`` and select the
newest one so subsequent snapshots/actions hit the right page.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

# Format from playwright-core renderTabsMarkdown:
#   - 0: (current) [Title](https://...)
#   - 1: [Other](https://...)
_TAB_LINE_RE = re.compile(
    r"^- (\d+):( \(current\))? \[(.*?)\]\((.*?)\)(?: \[crashed\])?\s*$",
    re.MULTILINE,
)

# Actions that commonly open a new tab / window.
_TAB_WATCH_ACTIONS = frozenset({
    "click",
    "browser_click",
})

CallToolFn = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


def parse_mcp_tabs(text: str) -> list[dict[str, Any]]:
    """Parse MCP browser_tabs / Open tabs markdown into structured rows."""
    tabs: list[dict[str, Any]] = []
    for m in _TAB_LINE_RE.finditer(text or ""):
        tabs.append({
            "index": int(m.group(1)),
            "current": bool(m.group(2)),
            "title": m.group(3) or "",
            "url": m.group(4) or "",
        })
    return tabs


def pick_new_tab_index(
    tabs_after: list[dict[str, Any]],
    *,
    count_before: int,
) -> Optional[int]:
    """Return index to select when a new tab appeared and is not current."""
    if len(tabs_after) <= count_before:
        return None
    newest = max(tabs_after, key=lambda t: t["index"])
    if newest.get("current"):
        return None
    return int(newest["index"])


def should_watch_for_new_tab(action: str) -> bool:
    return (action or "").strip().lower() in _TAB_WATCH_ACTIONS


async def list_tab_count(call_tool: CallToolFn) -> int:
    result = await call_tool("browser_tabs", {"action": "list"})
    tabs = parse_mcp_tabs(result.get("text") or "")
    return len(tabs) if tabs else 1


async def switch_to_new_tab_if_opened(
    call_tool: CallToolFn,
    *,
    count_before: int,
    result_text: str = "",
    settle_seconds: float = 0.5,
) -> bool:
    """If tab count grew after an action, select the newest non-current tab.

    Tries to parse Open tabs from the action result first; otherwise re-lists
    after a short settle (popups are often async).
    """
    import asyncio

    tabs = parse_mcp_tabs(result_text)
    index = pick_new_tab_index(tabs, count_before=count_before) if tabs else None

    if index is None:
        if settle_seconds > 0:
            await asyncio.sleep(settle_seconds)
        listed = await call_tool("browser_tabs", {"action": "list"})
        if not listed.get("success", True) and listed.get("error"):
            logger.warning("browser_tabs list failed: %s", listed.get("error") or listed.get("text"))
            return False
        tabs = parse_mcp_tabs(listed.get("text") or "")
        index = pick_new_tab_index(tabs, count_before=count_before)

    if index is None:
        return False

    selected = await call_tool("browser_tabs", {"action": "select", "index": index})
    if not selected.get("success", True):
        logger.warning(
            "Failed to select new tab index=%s: %s",
            index,
            selected.get("error") or selected.get("text"),
        )
        return False

    url = next((t.get("url") for t in tabs if t.get("index") == index), "")
    logger.info("Switched to new browser tab index=%s url=%s", index, url)
    return True
