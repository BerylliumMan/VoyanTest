"""Tests for app/websocket.py debug-mode methods — LogBroadcaster pause/resume and module-level state.

Covers:
  - LogBroadcaster.log_execution_paused / log_execution_resumed broadcast content
  - LogBroadcaster.get_pause_event / set_pause_decision lifecycle
  - Module-level _pause_events and _pause_decisions dicts
"""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from app.websocket import (
    LogBroadcaster,
    _pause_events,
    _pause_decisions,
    log_manager,
)


# ── fixture: clean module-level state before & after each test ──────────────

@pytest.fixture(autouse=True)
def clear_module_state():
    _pause_events.clear()
    _pause_decisions.clear()
    yield
    _pause_events.clear()
    _pause_decisions.clear()


# ══════════════════════════════════════════════════════════════════════════════
# log_execution_paused
# ══════════════════════════════════════════════════════════════════════════════

class TestLogExecutionPaused:

    @pytest.mark.asyncio
    async def test_broadcast_has_correct_type_and_fields(self):
        """调用 log_execution_paused 后 send_message 收到 execution_paused 消息。"""
        mock_send = AsyncMock()
        with patch.object(log_manager, "send_message", mock_send):
            await LogBroadcaster.log_execution_paused(1, 10, "click login", "element not found")

        mock_send.assert_awaited_once()
        call_run_id, payload = mock_send.await_args[0]
        assert call_run_id == 1
        assert payload["type"] == "execution_paused"
        assert payload["run_id"] == 1
        assert payload["step_id"] == 10
        assert payload["step_description"] == "click login"
        assert payload["reason"] == "element not found"
        assert payload["screenshot_path"] is None
        assert "timestamp" in payload
        assert "message" in payload

    @pytest.mark.asyncio
    async def test_default_options_are_retry_skip_abort(self):
        """未指定 options 时默认为 ["retry", "skip", "abort"]。"""
        mock_send = AsyncMock()
        with patch.object(log_manager, "send_message", mock_send):
            await LogBroadcaster.log_execution_paused(1, 5, "step", "reason")

        payload = mock_send.await_args[0][1]
        assert payload["options"] == ["retry", "skip", "abort"]

    @pytest.mark.asyncio
    async def test_custom_options_passed_through(self):
        """自定义 options 正确传递。"""
        mock_send = AsyncMock()
        with patch.object(log_manager, "send_message", mock_send):
            await LogBroadcaster.log_execution_paused(
                1, 5, "step", "reason", options=["retry", "abort"]
            )

        payload = mock_send.await_args[0][1]
        assert payload["options"] == ["retry", "abort"]

    @pytest.mark.asyncio
    async def test_screenshot_path_included_when_provided(self):
        """screenshot_path 参数存在时被正确携带。"""
        mock_send = AsyncMock()
        with patch.object(log_manager, "send_message", mock_send):
            await LogBroadcaster.log_execution_paused(
                2, 7, "verify title", "mismatch", screenshot_path="/tmp/err.png"
            )

        payload = mock_send.await_args[0][1]
        assert payload["screenshot_path"] == "/tmp/err.png"


# ══════════════════════════════════════════════════════════════════════════════
# log_execution_resumed
# ══════════════════════════════════════════════════════════════════════════════

class TestLogExecutionResumed:

    @pytest.mark.asyncio
    async def test_broadcast_has_correct_type_and_fields(self):
        """调用 log_execution_resumed 后 send_message 收到 execution_resumed 消息。"""
        mock_send = AsyncMock()
        with patch.object(log_manager, "send_message", mock_send):
            await LogBroadcaster.log_execution_resumed(1, step_id=10, decision="retry")

        mock_send.assert_awaited_once()
        call_run_id, payload = mock_send.await_args[0]
        assert call_run_id == 1
        assert payload["type"] == "execution_resumed"
        assert payload["run_id"] == 1
        assert payload["step_id"] == 10
        assert payload["decision"] == "retry"
        assert payload["new_description"] is None
        assert "timestamp" in payload
        assert "message" in payload

    @pytest.mark.asyncio
    async def test_default_parameters(self):
        """默认参数下 step_id=None, decision=""。"""
        mock_send = AsyncMock()
        with patch.object(log_manager, "send_message", mock_send):
            await LogBroadcaster.log_execution_resumed(99)

        payload = mock_send.await_args[0][1]
        assert payload["step_id"] is None
        assert payload["decision"] == ""
        assert payload["new_description"] is None

    @pytest.mark.asyncio
    async def test_edit_decision_with_new_description(self):
        """decision=edit 时 new_description 被正确携带。"""
        mock_send = AsyncMock()
        with patch.object(log_manager, "send_message", mock_send):
            await LogBroadcaster.log_execution_resumed(
                3, step_id=None, decision="edit", new_description="updated step desc"
            )

        payload = mock_send.await_args[0][1]
        assert payload["decision"] == "edit"
        assert payload["new_description"] == "updated step desc"


# ══════════════════════════════════════════════════════════════════════════════
# get_pause_event
# ══════════════════════════════════════════════════════════════════════════════

class TestGetPauseEvent:

    @pytest.mark.asyncio
    async def test_creates_new_event_for_unknown_run_id(self):
        """首次调用 get_pause_event 时创建新 asyncio.Event 并存入 _pause_events。"""
        event = await LogBroadcaster.get_pause_event(1)
        assert isinstance(event, asyncio.Event)
        assert 1 in _pause_events
        assert _pause_events[1] is event

    @pytest.mark.asyncio
    async def test_returns_same_event_for_known_run_id(self):
        """同一 run_id 两次调用返回同一个 Event 对象。"""
        e1 = await LogBroadcaster.get_pause_event(1)
        e2 = await LogBroadcaster.get_pause_event(1)
        assert e1 is e2

    @pytest.mark.asyncio
    async def test_different_run_ids_get_different_events(self):
        """不同 run_id 各自拥有独立的 Event。"""
        e1 = await LogBroadcaster.get_pause_event(1)
        e2 = await LogBroadcaster.get_pause_event(2)
        assert e1 is not e2
        assert 1 in _pause_events
        assert 2 in _pause_events


# ══════════════════════════════════════════════════════════════════════════════
# set_pause_decision
# ══════════════════════════════════════════════════════════════════════════════

class TestSetPauseDecision:

    @pytest.mark.asyncio
    async def test_stores_decision_and_sets_existing_event(self):
        """set_pause_decision 存储决策并触发已有的 Event。"""
        event = await LogBroadcaster.get_pause_event(1)          # 先创建 Event
        assert not event.is_set()

        await LogBroadcaster.set_pause_decision(1, "retry")
        assert _pause_decisions[1] == {"decision": "retry", "new_description": None}
        assert event.is_set()                               # Event 应被设置

    @pytest.mark.asyncio
    async def test_stores_with_edit_and_new_description(self):
        """decision=edit 时 new_description 被正确存储。"""
        await LogBroadcaster.get_pause_event(1)
        await LogBroadcaster.set_pause_decision(1, "edit", new_description="modified step")

        assert _pause_decisions[1] == {
            "decision": "edit",
            "new_description": "modified step",
        }

    @pytest.mark.asyncio
    async def test_no_event_exists_does_not_raise(self):
        """run_id 无对应 Event 时仅存储决策，不触发 Event（不报错）。"""
        await LogBroadcaster.set_pause_decision(99, "skip")

        assert _pause_decisions[99] == {"decision": "skip", "new_description": None}
        assert 99 not in _pause_events                         # 不会自动创建 Event


# ══════════════════════════════════════════════════════════════════════════════
# _pause_decisions lifecycle
# ══════════════════════════════════════════════════════════════════════════════

class TestPauseDecisions:

    @pytest.mark.asyncio
    async def test_overwrites_previous_decision(self):
        """同一 run_id 的后续决策覆盖之前的记录。"""
        await LogBroadcaster.set_pause_decision(1, "retry")
        assert _pause_decisions[1]["decision"] == "retry"

        await LogBroadcaster.set_pause_decision(1, "abort", new_description="give up")
        assert _pause_decisions[1] == {
            "decision": "abort",
            "new_description": "give up",
        }

    @pytest.mark.asyncio
    async def test_independent_across_run_ids(self):
        """不同 run_id 的决策互不影响。"""
        await LogBroadcaster.set_pause_decision(1, "retry")
        await LogBroadcaster.set_pause_decision(2, "skip", new_description="skip it")

        assert _pause_decisions[1] == {"decision": "retry", "new_description": None}
        assert _pause_decisions[2] == {"decision": "skip", "new_description": "skip it"}


# ══════════════════════════════════════════════════════════════════════════════
# _pause_events cleanup
# ══════════════════════════════════════════════════════════════════════════════

class TestPauseEventsLifecycle:

    @pytest.mark.asyncio
    async def test_pop_removes_event_from_dict(self):
        """从 _pause_events 中 pop 后不再存在。"""
        await LogBroadcaster.get_pause_event(1)
        assert 1 in _pause_events

        popped = _pause_events.pop(1)
        assert isinstance(popped, asyncio.Event)
        assert 1 not in _pause_events

    @pytest.mark.asyncio
    async def test_clear_removes_all_events(self):
        """clear 后 _pause_events 为空。"""
        await LogBroadcaster.get_pause_event(1)
        await LogBroadcaster.get_pause_event(2)
        assert len(_pause_events) == 2

        _pause_events.clear()
        assert len(_pause_events) == 0
