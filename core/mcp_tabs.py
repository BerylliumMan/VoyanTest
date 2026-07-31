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
    "click_blank",
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


def _is_blankish_tab_url(url: str) -> bool:
    u = (url or "").strip().lower()
    return (
        not u
        or u == "about:blank"
        or u.startswith("chrome://new-tab")
        or u.startswith("chrome://newtab")
    )


def pick_newest_tab_index(tabs: list[dict[str, Any]]) -> Optional[int]:
    """Highest-index tab, preferring a non-blank URL when present."""
    if not tabs:
        return None
    real = [t for t in tabs if not _is_blankish_tab_url(str(t.get("url") or ""))]
    pool = real or tabs
    return int(max(pool, key=lambda t: t["index"])["index"])


async def ensure_on_newest_tab(call_tool: CallToolFn) -> bool:
    """If multiple tabs exist and current is not the newest real tab, select it.

    Used after hybrid browser-use fallback: Playwright MCP often still points at
    the opener tab, and the next snapshot activates that tab in Chrome — looking
    like the browser "switched back" after opening a new tab.
    """
    listed = await call_tool("browser_tabs", {"action": "list"})
    if not listed.get("success", True) and listed.get("error"):
        logger.warning(
            "browser_tabs list failed (ensure newest): %s",
            listed.get("error") or listed.get("text"),
        )
        return False
    tabs = parse_mcp_tabs(listed.get("text") or "")
    if len(tabs) < 2:
        return False
    index = pick_newest_tab_index(tabs)
    if index is None:
        return False
    newest = next(t for t in tabs if t["index"] == index)
    if newest.get("current"):
        return False
    selected = await call_tool(
        "browser_tabs", {"action": "select", "index": index},
    )
    if not selected.get("success", True):
        logger.warning(
            "Failed to select newest tab index=%s: %s",
            index,
            selected.get("error") or selected.get("text"),
        )
        return False
    logger.info(
        "Ensured MCP focus on newest tab index=%s url=%s",
        index,
        newest.get("url") or "",
    )
    return True


async def switch_to_new_tab_if_opened(
    call_tool: CallToolFn,
    *,
    count_before: int,
    result_text: str = "",
    settle_seconds: float = 0.5,
    retries: int = 4,
    retry_interval: float = 0.4,
) -> bool:
    """If tab count grew after an action, select the newest non-current tab.

    Tries to parse Open tabs from the action result first; otherwise re-lists
    with settle + retries (popups are often async and slower than 0.5s).
    """
    import asyncio

    tabs = parse_mcp_tabs(result_text)
    index = pick_new_tab_index(tabs, count_before=count_before) if tabs else None

    attempts = max(1, int(retries))
    for attempt in range(attempts):
        if index is not None:
            break
        wait = settle_seconds if attempt == 0 else retry_interval
        if wait > 0:
            await asyncio.sleep(wait)
        listed = await call_tool("browser_tabs", {"action": "list"})
        if not listed.get("success", True) and listed.get("error"):
            logger.warning(
                "browser_tabs list failed: %s",
                listed.get("error") or listed.get("text"),
            )
            continue
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
