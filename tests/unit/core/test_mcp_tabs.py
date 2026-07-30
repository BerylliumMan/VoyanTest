"""Unit tests for MCP new-tab detection / selection helpers."""

import pytest

from core.mcp_tabs import (
    ensure_on_newest_tab,
    parse_mcp_tabs,
    pick_new_tab_index,
    should_watch_for_new_tab,
    switch_to_new_tab_if_opened,
)


SAMPLE = """
### Open tabs
- 0: (current) [Home](https://example.com/)
- 1: [Report](https://example.com/report)
"""


def test_parse_mcp_tabs():
    tabs = parse_mcp_tabs(SAMPLE)
    assert len(tabs) == 2
    assert tabs[0]["index"] == 0 and tabs[0]["current"] is True
    assert tabs[1]["index"] == 1 and tabs[1]["current"] is False
    assert tabs[1]["url"] == "https://example.com/report"


def test_pick_new_tab_index_when_grew():
    tabs = parse_mcp_tabs(SAMPLE)
    assert pick_new_tab_index(tabs, count_before=1) == 1


def test_pick_new_tab_index_no_growth():
    tabs = parse_mcp_tabs(SAMPLE)
    assert pick_new_tab_index(tabs, count_before=2) is None


def test_pick_new_tab_index_already_current():
    text = "- 0: [Home](https://example.com/)\n- 1: (current) [Report](https://example.com/report)\n"
    tabs = parse_mcp_tabs(text)
    assert pick_new_tab_index(tabs, count_before=1) is None


def test_should_watch_click_only():
    assert should_watch_for_new_tab("click")
    assert should_watch_for_new_tab("browser_click")
    assert not should_watch_for_new_tab("fill")
    assert not should_watch_for_new_tab("goto")


@pytest.mark.asyncio
async def test_switch_from_result_text():
    calls = []

    async def call_tool(name, args):
        calls.append((name, args))
        return {"success": True, "text": ""}

    ok = await switch_to_new_tab_if_opened(
        call_tool,
        count_before=1,
        result_text=SAMPLE,
        settle_seconds=0,
    )
    assert ok is True
    assert calls == [("browser_tabs", {"action": "select", "index": 1})]


@pytest.mark.asyncio
async def test_switch_after_settle_list():
    calls = []

    async def call_tool(name, args):
        calls.append((name, args))
        if args.get("action") == "list":
            return {"success": True, "text": SAMPLE}
        return {"success": True, "text": ""}

    ok = await switch_to_new_tab_if_opened(
        call_tool,
        count_before=1,
        result_text="",
        settle_seconds=0,
    )
    assert ok is True
    assert calls[0] == ("browser_tabs", {"action": "list"})
    assert calls[1] == ("browser_tabs", {"action": "select", "index": 1})


@pytest.mark.asyncio
async def test_ensure_on_newest_tab_selects_when_stale():
    calls = []

    async def call_tool(name, args):
        calls.append((name, args))
        if args.get("action") == "list":
            return {"success": True, "text": SAMPLE}
        return {"success": True, "text": ""}

    ok = await ensure_on_newest_tab(call_tool)
    assert ok is True
    assert ("browser_tabs", {"action": "select", "index": 1}) in calls


@pytest.mark.asyncio
async def test_ensure_on_newest_tab_noop_when_already_current():
    text = "- 0: [Home](https://example.com/)\n- 1: (current) [Report](https://example.com/report)\n"
    calls = []

    async def call_tool(name, args):
        calls.append((name, args))
        return {"success": True, "text": text}

    ok = await ensure_on_newest_tab(call_tool)
    assert ok is False
    assert all(c[1].get("action") != "select" for c in calls)

