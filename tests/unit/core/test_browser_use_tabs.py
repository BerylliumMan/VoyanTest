"""Unit tests for browser-use new-tab auto-switch helpers."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.browser_use_exec import (
    arm_browser_use_auto_switch_new_tabs,
    build_step_task,
    enable_browser_use_auto_switch_new_tabs,
    ensure_browser_use_on_newest_tab,
    list_browser_use_page_ids,
    switch_browser_use_to_newest_tab_if_opened,
)


def test_build_step_task_mentions_new_tab_switch():
    task = build_step_task(
        description="点击【详情】",
        expected_result="",
        step_order=1,
        base_url=None,
    )
    assert "新浏览器标签" in task or "新标签" in task


def test_build_step_task_mentions_semantic_fidelity():
    task = build_step_task(
        description="点击【提交】",
        expected_result="出现成功",
        step_order=2,
        base_url="https://example.com",
    )
    assert "忠实执行" in task
    assert "提交≠确定≠保存" in task
    assert "禁止臆造输入值" in task
    assert "BASE URL" in task


@pytest.mark.asyncio
async def test_list_browser_use_page_ids():
    session = SimpleNamespace(
        _cdp_get_all_pages=AsyncMock(
            return_value=[{"targetId": "aaa"}, {"targetId": "bbb"}]
        )
    )
    assert await list_browser_use_page_ids(session) == ["aaa", "bbb"]


def _awaitable_bus(dispatched: list):
    class Bus:
        def dispatch(self, event):
            dispatched.append(event)

            class _E:
                def __await__(self):
                    async def _done():
                        return "ok"
                    return _done().__await__()

            return _E()

    return Bus()


@pytest.mark.asyncio
async def test_switch_when_new_page_appeared():
    dispatched = []
    session = SimpleNamespace(
        agent_focus=SimpleNamespace(target_id="aaa"),
        event_bus=_awaitable_bus(dispatched),
        _cdp_get_all_pages=AsyncMock(
            return_value=[{"targetId": "aaa"}, {"targetId": "bbb"}]
        ),
        _voyantest_tab_auto={
            "armed": True,
            "preferred_target_id": None,
            "baseline_ids": {"aaa"},
        },
    )
    ok = await switch_browser_use_to_newest_tab_if_opened(
        session,
        page_ids_before=["aaa"],
        settle_seconds=0,
    )
    assert ok is True
    assert len(dispatched) == 1
    assert dispatched[0].target_id == "bbb"
    assert session._voyantest_tab_auto["preferred_target_id"] == "bbb"


@pytest.mark.asyncio
async def test_no_switch_when_no_new_page():
    session = SimpleNamespace(
        agent_focus=SimpleNamespace(target_id="aaa"),
        event_bus=MagicMock(),
        _cdp_get_all_pages=AsyncMock(return_value=[{"targetId": "aaa"}]),
    )
    ok = await switch_browser_use_to_newest_tab_if_opened(
        session,
        page_ids_before=["aaa"],
        settle_seconds=0,
    )
    assert ok is False
    session.event_bus.dispatch.assert_not_called()


@pytest.mark.asyncio
async def test_no_switch_without_baseline():
    """Without page_ids_before, do not guess via pages[-1] (CDP order unstable)."""
    session = SimpleNamespace(
        agent_focus=SimpleNamespace(target_id="aaa"),
        event_bus=MagicMock(),
        _cdp_get_all_pages=AsyncMock(
            return_value=[{"targetId": "bbb"}, {"targetId": "aaa"}]
        ),
    )
    ok = await switch_browser_use_to_newest_tab_if_opened(
        session,
        page_ids_before=None,
        settle_seconds=0,
    )
    assert ok is False
    session.event_bus.dispatch.assert_not_called()


@pytest.mark.asyncio
async def test_enable_and_arm_tab_auto_switch():
    handlers = []

    class Bus:
        def on(self, event_cls, handler):
            handlers.append((event_cls, handler))

    session = SimpleNamespace(
        event_bus=Bus(),
        agent_focus=None,
        _cdp_get_all_pages=AsyncMock(return_value=[{"targetId": "aaa"}]),
    )
    enable_browser_use_auto_switch_new_tabs(session)
    assert session._voyantest_tab_auto["armed"] is False
    assert handlers  # registered
    await arm_browser_use_auto_switch_new_tabs(session)
    assert session._voyantest_tab_auto["armed"] is True
    assert session._voyantest_tab_auto["baseline_ids"] == {"aaa"}


@pytest.mark.asyncio
async def test_ensure_without_preferred_does_not_guess_pages_last():
    """CDP may list opener last — must not flip back without preferred id."""
    dispatched = []
    session = SimpleNamespace(
        agent_focus=SimpleNamespace(target_id="bbb"),
        event_bus=_awaitable_bus(dispatched),
        _cdp_get_all_pages=AsyncMock(
            # opener listed last (unstable order)
            return_value=[{"targetId": "bbb"}, {"targetId": "aaa"}]
        ),
        _voyantest_tab_auto={
            "armed": True,
            "preferred_target_id": None,
            "baseline_ids": {"aaa"},
        },
    )
    ok = await ensure_browser_use_on_newest_tab(session, settle_seconds=0)
    assert ok is False
    assert dispatched == []


@pytest.mark.asyncio
async def test_ensure_on_preferred_tab_switches():
    dispatched = []
    session = SimpleNamespace(
        agent_focus=SimpleNamespace(target_id="aaa"),
        event_bus=_awaitable_bus(dispatched),
        _cdp_get_all_pages=AsyncMock(
            # preferred is first — old code using pages[-1] would wrongly pick aaa
            return_value=[{"targetId": "bbb"}, {"targetId": "aaa"}]
        ),
        _voyantest_tab_auto={
            "armed": True,
            "preferred_target_id": "bbb",
            "baseline_ids": {"aaa", "bbb"},
        },
    )
    ok = await ensure_browser_use_on_newest_tab(session, settle_seconds=0)
    assert ok is True
    assert dispatched[0].target_id == "bbb"
