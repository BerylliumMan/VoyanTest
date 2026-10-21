"""038 US2：覆盖判定防误归属。

规则：未分类描述不再被任意成功动作覆盖；只有显式 checklist_index 或
动作目标/文案/URL 与描述存在字面依据时才可覆盖。
"""
from __future__ import annotations

from core.goal_agent_loop import journal_entry_covers_checklist


def _entry(**kw):
    base = {"success": True, "action": "click", "element_desc": "", "value": "", "selector": ""}
    base.update(kw)
    return base


def test_unclassified_step_rejects_unrelated_click():
    """运行 445 等价：未分类"导出数据报表" + 一次导航点击 → 不得覆盖。"""
    assert not journal_entry_covers_checklist(
        _entry(action="click", element_desc="项目管理"),
        step_description="导出数据报表",
    )


def test_unclassified_step_accepts_literal_target():
    assert journal_entry_covers_checklist(
        _entry(action="click", element_desc="导出数据报表按钮"),
        step_description="导出数据报表",
    )


def test_unclassified_step_accepts_page_name_in_description():
    assert journal_entry_covers_checklist(
        _entry(action="click", element_desc="项目管理"),
        step_description="查看项目管理主要功能",
    )


def test_unclassified_step_accepts_explicit_checklist_index():
    assert journal_entry_covers_checklist(
        _entry(action="click", element_desc="随便", _explicit_checklist=3),
        step_description="验证列表显示正确",
    )


def test_unclassified_step_rejects_generic_only_overlap():
    assert not journal_entry_covers_checklist(
        _entry(action="click", element_desc="数据管理"),
        step_description="验证列表显示正确",
    )


def test_click_step_accepts_coordinate_hit_with_literal_evidence():
    assert journal_entry_covers_checklist(
        _entry(action="click_xy", value="120,340",
               resolved={"name": "新增", "text": "新增", "tag": "button"}),
        step_description="点击【新增】按钮",
    )


def test_click_step_rejects_coordinate_hit_without_literal_evidence():
    assert not journal_entry_covers_checklist(
        _entry(action="click_xy", value="120,340",
               resolved={"name": "删除", "text": "删除", "tag": "button"}),
        step_description="点击【新增】按钮",
    )


def test_click_step_still_rejects_fill():
    assert not journal_entry_covers_checklist(
        _entry(action="fill", element_desc="新增", value="x"),
        step_description="点击【新增】按钮",
    )


def test_fill_step_still_accepts_fill():
    assert journal_entry_covers_checklist(
        _entry(action="fill", value="hello"),
        step_description="在【名称】输入 hello",
    )


def test_wait_assert_step_still_rejects_click():
    assert not journal_entry_covers_checklist(
        _entry(action="click", element_desc="状态：已刷新"),
        step_description="断言页面出现【状态：已刷新】",
    )


def test_fetch_still_covers_export_step():
    assert journal_entry_covers_checklist(
        _entry(action="fetch", value="/api/export.csv"),
        step_description="导出数据报表",
    )


def test_fetch_does_not_cover_unrelated_business_step():
    """038 实测（批次 576）：fetch /openapi.json 曾把「整理交付材料」记为通过。"""
    assert not journal_entry_covers_checklist(
        _entry(action="fetch", value="http://localhost:8002/openapi.json"),
        step_description="整理交付材料",
    )


def test_screenshot_never_covers():
    assert not journal_entry_covers_checklist(
        _entry(action="screenshot"), step_description="导出数据报表"
    )


def test_sanitize_journal_keeps_literal_match_and_drops_unrelated():
    from core.script_synthesize import sanitize_journal_for_synth

    steps = [
        {"step_order": 1, "description": "查看项目管理主要功能"},
        {"step_order": 2, "description": "导出数据报表"},
    ]
    journal = [
        {"success": True, "action": "click", "checklist_index": 1,
         "element_desc": "项目管理", "value": ""},
        {"success": True, "action": "click", "checklist_index": 2,
         "element_desc": "项目管理", "value": ""},
    ]
    kept = sanitize_journal_for_synth(journal, steps)
    assert [k.get("checklist_index") for k in kept] == [1]


def test_coordinate_click_covers_point_select_step():
    """038 S1：点选验证码步骤（未分类）应被 click_xy 覆盖。"""
    assert journal_entry_covers_checklist(
        _entry(action="click_xy", value="338,189"),
        step_description="按图中的提示顺序点击三个目标图形（点选验证码）",
    )


def test_coordinate_click_does_not_cover_unrelated_business_step():
    assert not journal_entry_covers_checklist(
        _entry(action="click_xy", value="338,189"),
        step_description="整理交付材料",
    )


def test_coverage_reason_codes():
    from core.goal_agent_loop import coverage_reason

    assert coverage_reason({"success": False, "action": "click"}, step_description="x") == "action_failed"
    assert coverage_reason({"success": True, "action": "click", "_explicit_checklist": 2},
                           step_description="x") == "explicit_checklist"
    assert coverage_reason({"success": True, "action": "click", "element_desc": "项目管理"},
                           step_description="导出数据报表") == "unclassified_no_evidence"
    assert coverage_reason({"success": True, "action": "click", "element_desc": "导出数据报表"},
                           step_description="导出数据报表") == "literal_match"
