"""034 实测发现回归：断言极性守卫 —— wait(gone) 不得冒充正向断言（假通过）。

线上实例（2026-10-10，case 670 / 批次 468）：
步骤「断言页面包含【这段文字绝对不存在-034校验】」本应失败，
模型将其反转为负向语义并执行 wait(wait_mode=gone)，因文本确实不存在而"成功"，
完成判定把它当作覆盖 → 整条 run 假通过（status=passed）。
修复：覆盖判定引入极性守卫（正向断言 ↔ 负向等待必须一致）。
"""
from core.goal_agent_loop import journal_entry_covers_checklist


def test_wait_gone_must_not_cover_positive_assert():
    desc = "断言页面包含【这段文字绝对不存在-034校验】"
    entry = {
        "success": True, "action": "wait",
        "value": "这段文字绝对不存在-034校验", "wait_mode": "gone",
    }
    assert journal_entry_covers_checklist(entry, step_description=desc) is False


def test_wait_text_covers_positive_assert():
    entry = {"success": True, "action": "wait", "value": "Products", "wait_mode": "text"}
    assert journal_entry_covers_checklist(
        entry, step_description="断言页面包含【Products】",
    ) is True


def test_wait_gone_covers_negative_step():
    entry = {"success": True, "action": "wait", "value": "Login", "wait_mode": "gone"}
    assert journal_entry_covers_checklist(
        entry, step_description="等待【Login】消失",
    ) is True


def test_wait_text_must_not_cover_negative_step():
    entry = {"success": True, "action": "wait", "value": "Login", "wait_mode": "text"}
    assert journal_entry_covers_checklist(
        entry, step_description="等待【Login】消失",
    ) is False


def test_assert_text_still_covers_positive_assert():
    entry = {"success": True, "action": "assert_text", "value": "Swag Labs"}
    assert journal_entry_covers_checklist(
        entry, step_description="断言页面包含【Swag Labs】",
    ) is True


def test_wait_without_mode_keeps_legacy_behavior_for_positive_assert():
    # 旧合成 journal 无 wait_mode：正向断言仍按文本匹配覆盖（保持向后兼容）
    entry = {"success": True, "action": "wait", "value": "Products"}
    assert journal_entry_covers_checklist(
        entry, step_description="断言页面包含【Products】",
    ) is True
