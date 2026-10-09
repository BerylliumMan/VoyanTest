# tests/unit/test_structured_step.py
"""StructuredStep normalize / parse / bind helpers."""

from core.step_normalize import (
    coerce_structured_step,
    parse_instant_to_structured,
    render_structured_step,
    sanitize_ui_step,
    structured_step_is_complete,
    validate_structured_step_fields,
)
from core.step_intent import structured_to_intent
from app.gen.agents.validator import validate_test_cases


def test_parse_and_render_roundtrip_click():
    st = parse_instant_to_structured("点击【登录】")
    assert st and st["action"] == "click"
    assert st["target_name"] == "登录"
    assert render_structured_step(st) == "点击【登录】"


def test_parse_fill():
    st = parse_instant_to_structured("在【用户名】输入 admin")
    assert st == {
        "action": "fill",
        "target_name": "用户名",
        "target_role": "textbox",
        "value": "admin",
    }


def test_coerce_object_step():
    st = coerce_structured_step({
        "action": "click",
        "target_name": "单位下拉框",
        "target_role": "button",
    })
    assert st["target_name"] == "单位"
    assert structured_step_is_complete(st)


def test_validator_rejects_ellipsis():
    errs = validate_structured_step_fields(
        {"action": "click", "target_name": "辅助（高检...）"},
        index=0,
    )
    assert any("省略号" in e for e in errs)


def test_validator_rejects_control_type_and_missing_action():
    errs = validate_structured_step_fields(
        {"action": "click", "target_name": "图标"},
        index=0,
    )
    assert any("控件类型词" in e for e in errs)
    errs2 = validate_structured_step_fields({"target_name": "登录"}, index=0)
    assert any("缺少 action" in e for e in errs2)


def test_validator_icon_click_requires_hint():
    errs = validate_structured_step_fields(
        {"action": "icon_click"},
        index=0,
    )
    assert any("icon_hint" in e for e in errs)


def test_validate_test_cases_ui_hard_gate():
    bad = [{
        "title": "坏用例",
        "module": "登录",
        "steps": [
            {"action": "click", "target_name": "智能辅助（高检...）"},
        ],
        "expected": [""],
    }]
    result = validate_test_cases(bad, [], require_structured=True)
    assert result["invalid_count"] >= 1
    assert any("省略号" in w for w in result.get("warnings", []))


def test_validator_rejects_observe_style_steps():
    bad = [{
        "title": "观察体用例",
        "module": "购物车",
        "steps": [
            {"action": "click", "target_name": "Add to cart"},
            {"action": "click", "target_name": "购物车", "description": "观察该按钮的文字变化"},
            {"action": "click", "target_name": "徽标", "description": "观察右上角徽标数量"},
        ],
        "expected": [""],
    }]
    result = validate_test_cases(bad, [], require_structured=True)
    assert result["invalid_count"] >= 1
    assert any("观察体" in w for w in result.get("warnings", []))


def test_validator_accepts_executable_style():
    good = [{
        "title": "登录验证",
        "module": "登录",
        "steps": [
            {"action": "fill", "target_name": "Username", "value": "standard_user",
             "description": "在【Username】输入框输入「standard_user」"},
            {"action": "click", "target_name": "Login",
             "description": "点击【Login】登录按钮"},
            {"action": "assert_text", "target_name": "Products", "value": "Products",
             "description": "断言页面包含【Products】"},
        ],
        "expected": ["跳转到商品列表页"],
    }]
    result = validate_test_cases(good, [], require_structured=True)
    assert result["invalid_count"] == 0


def test_normalize_tc_structured_objects():
    from app.gen.response_parser import _normalize_tc_item

    tc = _normalize_tc_item({
        "title": "登录主路径",
        "module": "登录",
        "priority": "P0",
        "steps": [
            {"action": "fill", "target_name": "用户名", "value": "admin"},
            {"action": "click", "target_name": "登录", "target_role": "button"},
        ],
        "expected": ["", "进入首页"],
    })
    assert "在【用户名】输入 admin" in tc["test_steps"]
    assert len(tc["structured_steps"]) == 2
    assert tc["structured_steps"][0]["action"] == "fill"


def test_normalize_legacy_string_steps():
    from app.gen.response_parser import _normalize_tc_item

    tc = _normalize_tc_item({
        "title": "旧字符串步骤",
        "module": "登录",
        "steps": ["在【用户名】输入 admin", "点击【登录】"],
        "expected": ["", "进首页"],
    })
    assert tc["structured_steps"][0]["action"] == "fill"
    assert tc["structured_steps"][1]["action"] == "click"


def test_structured_to_intent_skips_incomplete():
    assert structured_to_intent({"action": "click"}) is None
    intent = structured_to_intent({"action": "click", "target_name": "确定", "target_role": "button"})
    assert intent is not None
    assert intent.action == "click"
    assert intent.target_name == "确定"


def test_sanitize_still_works():
    assert sanitize_ui_step("点击登录按钮") == "点击【登录】"


# ── 描述优先编辑：上传/拖拽/弹窗/双击/右键 ────────────────────────────────


def test_parse_upload_with_path():
    st = parse_instant_to_structured("上传文件 /tmp/a.png")
    assert st == {"action": "upload", "target_name": "文件", "value": "/tmp/a.png"}
    assert structured_step_is_complete(st)
    assert render_structured_step(st) == "上传文件 /tmp/a.png"


def test_parse_upload_with_field_target():
    st = parse_instant_to_structured("在【附件上传】上传文件 /tmp/a.png")
    assert st["action"] == "upload"
    assert st["target_name"] == "附件上传"
    assert st["value"] == "/tmp/a.png"
    assert render_structured_step(st) == "在【附件上传】上传文件 /tmp/a.png"


def test_parse_upload_multiple_paths_chinese_sep():
    st = parse_instant_to_structured("上传文件 /tmp/a.png、/tmp/b.png")
    assert st["value"] == "/tmp/a.png,/tmp/b.png"


def test_parse_upload_no_path_incomplete():
    st = parse_instant_to_structured("上传头像文件")
    assert st["action"] == "upload"
    assert not st.get("value")
    assert not structured_step_is_complete(st)


def test_parse_drop_with_path():
    st = parse_instant_to_structured("拖拽文件 /tmp/a.png 到【上传区】")
    assert st["action"] == "drop"
    assert st["target_name"] == "上传区"
    assert st["value"] == "/tmp/a.png"
    assert structured_step_is_complete(st)
    assert render_structured_step(st) == "拖拽文件 /tmp/a.png 到【上传区】"


def test_parse_dialog_accept_dismiss():
    assert parse_instant_to_structured("接受弹窗")["value"] == "accept"
    assert parse_instant_to_structured("接受弹窗：确认删除")["value"] == "accept:确认删除"
    assert parse_instant_to_structured("拒绝弹窗")["value"] == "dismiss"
    assert parse_instant_to_structured("关闭原生弹窗")["value"] == "dismiss"
    # 泛化的「关闭弹窗」不劫持为原生弹窗（仍交给通用解析）
    assert parse_instant_to_structured("关闭弹窗") is None


def test_parse_double_and_right_click():
    d = parse_instant_to_structured("双击【商品卡片】")
    assert d["action"] == "double_click"
    assert structured_step_is_complete(d)
    r = parse_instant_to_structured("右键点击【行记录】")
    assert r["action"] == "right_click"
    assert structured_step_is_complete(r)


def test_parse_select_file_still_select():
    st = parse_instant_to_structured("选择头像文件")
    assert st and st["action"] == "select"


def test_coerce_alias_file_upload():
    st = coerce_structured_step({"action": "browser_file_upload", "value": "/tmp/a.png"})
    assert st and st["action"] == "upload"
    assert st["value"] == "/tmp/a.png"


def test_validate_upload_requires_path():
    errs = validate_structured_step_fields({"action": "upload", "target_name": "文件"}, index=0)
    assert any("文件路径" in e for e in errs)


def test_selector_candidates_upload_file_input():
    from core.step_intent import selector_tool_call_candidates

    tcs = selector_tool_call_candidates(
        {"action": "upload", "target_name": "文件", "value": "/tmp/a.png"}
    )
    assert tcs and tcs[0].action == "upload"
    assert tcs[0].selector == 'input[type="file"]'
    assert tcs[0].value == "/tmp/a.png"


def test_intent_dialog_needs_no_ref():
    from core.step_intent import intent_to_tool_call

    intent = structured_to_intent({"action": "dialog", "value": "dismiss"})
    assert intent is not None and intent.action == "dialog"
    tc = intent_to_tool_call(intent, ref=None)
    assert tc.action == "dialog"
    assert tc.selector is None
    assert tc.value == "dismiss"
