"""Tests for browser-use RUN_LOG forwarding and progress helpers."""
from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest

from agent.models import WSMessageType
from agent.router import _log_agent_run_log
from core.browser_use_exec import (
    _emit_progress,
    _make_new_step_callback,
    _summarize_agent_output,
    _truncate,
    attach_browser_use_log_handler,
    detach_browser_use_log_handlers,
)


def test_ws_message_type_has_run_log():
    assert WSMessageType.RUN_LOG.value == "run_log"
    assert WSMessageType.RUN_LOG not in (
        WSMessageType.STEP_RESULT,
        WSMessageType.RUN_COMPLETE,
    )


def test_log_agent_run_log_formats_and_levels(caplog):
    with caplog.at_level(logging.INFO, logger="agent.run_log"):
        _log_agent_run_log(
            "Agent-1",
            "run-42",
            {
                "level": "info",
                "message": "--- Step 1 start: 点击登录 ---",
                "backend": "browser_use",
                "step_order": 1,
            },
        )
    assert any("Agent-1" in r.message for r in caplog.records)
    assert any("run-42" in r.message for r in caplog.records)
    assert any("step=1" in r.message for r in caplog.records)
    assert any("点击登录" in r.message for r in caplog.records)


def test_log_agent_run_log_skips_empty():
    # Should not raise
    _log_agent_run_log("a", "r", {"level": "info", "message": "  "})
    _log_agent_run_log("a", "r", {})


def test_truncate():
    assert _truncate("abc", 10) == "abc"
    assert len(_truncate("x" * 50, 20)) == 20
    assert _truncate("x" * 50, 20).endswith("…")


def test_emit_progress_swallows_callback_errors():
    def boom(_msg: str) -> None:
        raise RuntimeError("nope")

    _emit_progress(boom, "hello")  # must not raise
    _emit_progress(None, "hello")


def test_summarize_agent_output():
    state = MagicMock()
    state.thinking = "looking for button"
    state.next_goal = "click login"
    output = MagicMock()
    output.current_state = state
    output.action = [MagicMock(name="click")]
    text = _summarize_agent_output(output)
    assert "goal=" in text or "think=" in text or "actions=" in text


def test_make_new_step_callback_none_without_progress():
    assert _make_new_step_callback(None, step_order=1) is None


def test_make_new_step_callback_invokes_progress():
    seen: list[str] = []
    cb = _make_new_step_callback(seen.append, step_order=3)
    assert cb is not None
    state = MagicMock()
    output = MagicMock()
    output.current_state = MagicMock(thinking="t", next_goal="g")
    output.action = []
    cb(state, output, 2)
    assert seen
    assert "NL step 3" in seen[0]
    assert "agent-turn 2" in seen[0]


def test_attach_detach_log_handlers():
    seen: list[str] = []
    attached = attach_browser_use_log_handler(on_progress=seen.append)
    assert attached
    logging.getLogger("browser_use").info("hello-from-bu")
    detach_browser_use_log_handlers(attached)
    assert any("hello-from-bu" in s for s in seen)
    # After detach, further logs should not append
    n = len(seen)
    logging.getLogger("browser_use").info("after-detach")
    assert len(seen) == n
