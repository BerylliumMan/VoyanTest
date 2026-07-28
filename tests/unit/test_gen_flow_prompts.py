"""Unit tests for flow-manual generation prompt key helpers."""
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
    assert min_tcs_per_item(["tc_generate_ui"]) == 3
    assert min_tcs_per_item(None) == 3


def test_flow_prompts_require_boxed_ui_and_vision():
    from app.gen.prompts import FP_EXTRACT_FLOW_PROMPT, TC_GENERATE_FLOW_PROMPT

    for text in (FP_EXTRACT_FLOW_PROMPT, TC_GENERATE_FLOW_PROMPT):
        assert "红框" in text or "色框" in text
        assert "截图" in text


def test_ui_and_flow_tc_prompts_forbid_dropdown_control_words():
    from app.gen.prompts import TC_GENERATE_FLOW_PROMPT, TC_GENERATE_UI_PROMPT

    for text in (TC_GENERATE_UI_PROMPT, TC_GENERATE_FLOW_PROMPT):
        assert "在【单位】中选择" in text or "在【字段】中选择" in text or "在【字段名】中选择" in text
        assert "下拉框选择" in text  # appears in forbidden examples
        assert "禁止" in text


def test_flow_tc_expected_must_come_from_document():
    from app.gen.prompts import TC_GENERATE_FLOW_PROMPT

    assert '""' in TC_GENERATE_FLOW_PROMPT or "空字符串" in TC_GENERATE_FLOW_PROMPT
    assert "禁止自行编写" in TC_GENERATE_FLOW_PROMPT or "禁止**自行编写" in TC_GENERATE_FLOW_PROMPT
    assert "文档未写明预期" in TC_GENERATE_FLOW_PROMPT  # mentioned as forbidden placeholder


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
