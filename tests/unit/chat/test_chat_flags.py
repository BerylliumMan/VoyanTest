"""044：增强机制开关回归——关闭后工具清单与预算行为恢复原状。"""

import pytest

from app.config import get_settings
from core.tools.loop import default_tool_filter


@pytest.mark.parametrize(
    "flag,tool",
    [
        ("chat_plan_mode_enabled", "enter_plan_mode"),
        ("chat_plan_mode_enabled", "exit_plan_mode"),
        ("chat_subagent_enabled", "agent"),
    ],
)
def test_tool_gated_by_flag(monkeypatch, flag, tool):
    settings = get_settings()
    monkeypatch.setattr(settings, flag, False)
    assert default_tool_filter(tool) is False
    monkeypatch.setattr(settings, flag, True)
    assert default_tool_filter(tool) is True


def test_memory_disabled_skips_injection(monkeypatch):
    from core.chat import orchestrator as orch

    settings = get_settings()
    monkeypatch.setattr(settings, "chat_memory_enabled", False)

    class Conv:
        project_id = 1

    assert orch._memory_block(Conv(), []) is None


def test_compact_disabled_returns_original(monkeypatch):
    from core.chat import orchestrator as orch

    settings = get_settings()
    monkeypatch.setattr(settings, "chat_compact_enabled", False)

    class Conv:
        summary = None
        summarized_until_message_id = None

    msgs = [{"role": "user", "content": "x" * 100000}]
    summary, out = __import__("asyncio").run(orch._maybe_update_summary(None, Conv(), msgs, None))
    assert summary is None
    assert out == msgs
