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
