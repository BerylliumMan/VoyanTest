"""044 US3：system-reminder 调度（先红）。"""

from core.chat.reminders import Reminder, ReminderTracker, wrap


def test_wrap_escapes_nested_tags():
    text = wrap(Reminder(source="x", content="a <system-reminder> b"))
    assert text.count("<system-reminder>") == 1
    assert "&lt;system-reminder>" in text


def test_repeat_reminder_fires_once_at_threshold():
    t = ReminderTracker(repeat_threshold=3)
    sig = ["search_cases::{}"]
    assert t.on_tool_round(1, sig) == []
    assert t.on_tool_round(2, sig) == []
    out = t.on_tool_round(3, sig)
    assert len(out) == 1 and "重复" in out[0].content
    assert t.on_tool_round(4, sig) == []


def test_todo_reminder_has_interval():
    t = ReminderTracker(todo_interval=3)
    fired = [len(t.on_tool_round(i, [])) for i in range(1, 10)]
    assert fired.count(1) >= 2
    assert fired[0] == 0
