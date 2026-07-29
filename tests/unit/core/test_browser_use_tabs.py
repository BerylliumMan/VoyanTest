"""Unit tests for browser-use new-tab auto-switch helpers."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.browser_use_exec import (
    arm_browser_use_auto_switch_new_tabs,
    build_step_task,
    enable_browser_use_auto_switch_new_tabs,
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


@pytest.mark.asyncio
async def test_switch_when_new_page_appeared():
    dispatched = []

    class FakeEvent:
        def __await__(self):
            if False:
                yield None
            return None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class Bus:
        def dispatch(self, event):
            dispatched.append(event)
            fut = AsyncMock()
            # awaitable: return a simple object that is awaitable
            class _E:
                def __await__(self):
                    async def _done():
                        return "ok"
                    return _done().__await__()

            return _E()

    session = SimpleNamespace(
        agent_focus=SimpleNamespace(target_id="aaa"),
        event_bus=Bus(),
        _cdp_get_all_pages=AsyncMock(
            return_value=[{"targetId": "aaa"}, {"targetId": "bbb"}]
        ),
    )
    ok = await switch_browser_use_to_newest_tab_if_opened(
        session,
        page_ids_before=["aaa"],
        settle_seconds=0,
    )
    assert ok is True
    assert len(dispatched) == 1
    assert dispatched[0].target_id == "bbb"


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


def test_enable_and_arm_tab_auto_switch():
    handlers = []

    class Bus:
        def on(self, event_cls, handler):
            handlers.append((event_cls, handler))

    session = SimpleNamespace(event_bus=Bus(), agent_focus=None)
    enable_browser_use_auto_switch_new_tabs(session)
    assert session._voyantest_tab_auto["armed"] is False
    assert handlers  # registered
    arm_browser_use_auto_switch_new_tabs(session)
    assert session._voyantest_tab_auto["armed"] is True
