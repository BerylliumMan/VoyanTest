"""044 US2：计划模式状态机（先红）。"""

from core.chat.plan_mode import (
    PlanState,
    enter_plan_mode,
    exit_plan_mode,
    plan_reminder,
)


def test_enter_plan_mode_sets_active_and_phase():
    s = enter_plan_mode(PlanState())
    assert s.active is True and s.phase == "understand"
    assert "理解" in plan_reminder(s)


def test_plan_reminder_none_when_inactive():
    assert plan_reminder(PlanState()) is None


def test_exit_plan_mode_returns_plan_and_deactivates():
    s = exit_plan_mode(PlanState(active=True, phase="approve"), "1. 查用例\n2. 执行")
    assert s.active is False
    assert "查用例" in s.plan_text


def test_plan_reminder_always_offers_exit_path():
    text = plan_reminder(PlanState(active=True, phase="design"))
    assert "exit_plan_mode" in text or "退出" in text
    assert "AskUserQuestion" in text or "exit_plan_mode" in text
