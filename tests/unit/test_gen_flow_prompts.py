"""Unit tests for generation prompt key helpers and coverage contracts."""
from app.gen.prompts import (
    min_tcs_per_item,
    pick_fp_prompt_key,
    pick_tc_prompt_key,
)


def test_pick_fp_prompt_key_flow():
    assert pick_fp_prompt_key(["fp_extract_flow", "tc_generate_flow"]) == "fp_extract_flow"
    assert pick_fp_prompt_key(["fp_extract", "tc_generate"]) == "fp_extract"
    assert pick_fp_prompt_key(None) == "fp_extract"


def test_pick_tc_prompt_key_priority():
    assert pick_tc_prompt_key(["tc_generate_flow", "tc_generate_ui"]) == "tc_generate_flow"
    assert pick_tc_prompt_key(["tc_generate_ui", "fp_extract"]) == "tc_generate_ui"
    assert pick_tc_prompt_key(["tc_generate"]) == "tc_generate"
    assert pick_tc_prompt_key([]) == "tc_generate"


def test_min_tcs_per_item_flow_vs_default():
    assert min_tcs_per_item(["tc_generate_flow"]) == 1
    assert min_tcs_per_item(["fp_extract_flow"]) == 1
    assert min_tcs_per_item(tc_prompt_key="tc_generate_flow") == 1
    assert min_tcs_per_item(["tc_generate_ui"]) == 2
    assert min_tcs_per_item(None) == 2


def test_tc_generate_uses_type_driven_scenarios_not_forced_triad():
    from app.gen.prompts import TC_GENERATE_PROMPT, TC_GENERATE_UI_PROMPT

    for text in (TC_GENERATE_PROMPT, TC_GENERATE_UI_PROMPT):
        assert "禁止" in text and "机械" in text
        assert "主路径" in text
    assert "至少覆盖三类" not in TC_GENERATE_PROMPT
    assert "正常流程 + 1 条异常流程 + 1 条边界" not in TC_GENERATE_PROMPT
    assert "组合查询" in TC_GENERATE_PROMPT
    assert "scenario_type\":" in TC_GENERATE_PROMPT or "scenario_type" in TC_GENERATE_PROMPT


def test_flow_prompts_require_boxed_ui_and_vision():
    from app.gen.prompts import FP_EXTRACT_FLOW_PROMPT, TC_GENERATE_FLOW_PROMPT

    for text in (FP_EXTRACT_FLOW_PROMPT, TC_GENERATE_FLOW_PROMPT):
        assert "红框" in text or "色框" in text
        assert "截图" in text


def test_ui_and_flow_tc_prompts_forbid_dropdown_control_words():
    from app.gen.prompts import TC_GENERATE_FLOW_PROMPT, TC_GENERATE_UI_PROMPT

    for text in (TC_GENERATE_UI_PROMPT, TC_GENERATE_FLOW_PROMPT):
        assert (
            "在【单位】中选择" in text
            or "在【字段】中选择" in text
            or "在【字段名】中选择" in text
            or "在【字段标签】中选择" in text
        )
        assert "下拉框选择" in text  # appears in forbidden examples
        assert "禁止" in text


def test_flow_tc_expected_must_come_from_document():
    from app.gen.prompts import TC_GENERATE_FLOW_PROMPT

    assert '""' in TC_GENERATE_FLOW_PROMPT or "空字符串" in TC_GENERATE_FLOW_PROMPT
    assert "严禁" in TC_GENERATE_FLOW_PROMPT and (
        "空编号" in TC_GENERATE_FLOW_PROMPT or "只有序号" in TC_GENERATE_FLOW_PROMPT
    )
    assert "文档未写明" in TC_GENERATE_FLOW_PROMPT


def test_fp_extract_requires_per_field_query_and_combo():
    from app.gen.prompts import FP_EXTRACT_PROMPT

    assert "每个查询字段" in FP_EXTRACT_PROMPT or "每个查询条件" in FP_EXTRACT_PROMPT
    assert "组合查询" in FP_EXTRACT_PROMPT
    assert "禁止" in FP_EXTRACT_PROMPT and "揉成一个粗项" in FP_EXTRACT_PROMPT


def test_tc_generate_requires_pairwise_combo_scenario():
    from app.gen.prompts import TC_GENERATE_PROMPT

    assert "组合查询" in TC_GENERATE_PROMPT
    assert "两两组合" in TC_GENERATE_PROMPT
    assert "笛卡尔" in TC_GENERATE_PROMPT
    assert "机械" in TC_GENERATE_PROMPT


def test_ui_prompts_forbid_bare_page_load_wait():
    from app.gen.prompts import (
        TC_GENERATE_FLOW_PROMPT,
        TC_GENERATE_UI_PROMPT,
        _UI_STEP_CONTRACT,
    )

    for text in (_UI_STEP_CONTRACT, TC_GENERATE_UI_PROMPT, TC_GENERATE_FLOW_PROMPT):
        assert "禁止" in text and "等待页面加载完成" in text
        # Examples must prefer visible-text waits, not bare load waits as good path
        assert "等待【" in text


def test_ui_prompts_require_disambiguation_and_empty_mid_expected():
    from app.gen.prompts import TC_GENERATE_UI_PROMPT, _UI_STEP_CONTRACT

    assert "同名" in _UI_STEP_CONTRACT or "消歧" in _UI_STEP_CONTRACT
    assert "Add to cart" in _UI_STEP_CONTRACT or "相同文案" in _UI_STEP_CONTRACT
    assert '""' in TC_GENERATE_UI_PROMPT
    assert "中间" in TC_GENERATE_UI_PROMPT or "默认" in TC_GENERATE_UI_PROMPT


def test_sanitize_rewrites_bare_page_load_wait():
    from app.gen.response_parser import _sanitize_ui_step

    assert _sanitize_ui_step("等待页面加载完成") == "等待页面稳定"
    assert _sanitize_ui_step("等待加载完成") == "等待页面稳定"
    assert _sanitize_ui_step("等待【Login】出现") == "等待【Login】出现"
    assert _sanitize_ui_step("密码输入【Abc123】") == "在【密码】输入 Abc123"


def test_sanitize_strips_title_scenario_suffix():
    from app.gen.response_parser import _sanitize_tc_title

    assert _sanitize_tc_title("正确手机号验证码登录-正常") == "正确手机号验证码登录"
    assert _sanitize_tc_title("验证码错误无法登录-异常") == "验证码错误无法登录"
    assert _sanitize_tc_title("验证码过期后登录—边界场景") == "验证码过期后登录"
    assert _sanitize_tc_title("姓名与状态组合查询") == "姓名与状态组合查询"
    assert _sanitize_tc_title("登录-正常流程") == "登录"


def test_tc_prompts_forbid_title_scenario_suffix():
    from app.gen.prompts import TC_GENERATE_PROMPT, TC_GENERATE_UI_PROMPT

    for text in (TC_GENERATE_PROMPT, TC_GENERATE_UI_PROMPT):
        assert "禁止" in text and "-正常" in text
    assert '"title":"正确手机号验证码登录-正常"' not in TC_GENERATE_PROMPT
    assert '"title":"正确手机号验证码登录"' in TC_GENERATE_PROMPT


def test_compact_parts_keep_images_prefers_images():
    from app.gen.feature_extractor import compact_parts_keep_images

    parts = [
        {"type": "text", "text": "说明文字" * 500},
        {"type": "image", "ext": "png", "b64": "aaa"},
        {"type": "text", "text": "更多文字" * 500},
        {"type": "image", "ext": "png", "b64": "bbb"},
    ]
    # Tiny budget: should still try to keep at least one image
    out = compact_parts_keep_images(parts, budget=2000)
    assert any(p.get("type") == "image" for p in out)
