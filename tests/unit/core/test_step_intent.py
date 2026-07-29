"""Unit tests for Midscene-inspired Intent matching and plan cache helpers."""

from __future__ import annotations

from core.locator_memory import build_plan_blob, get_plan_steps
from core.step_intent import (
    StepIntent,
    extract_label_hints,
    intent_to_tool_call,
    match_intent_candidates,
)
from core.verification_strategy import VerificationStrategy


SNAP = """
Page URL: https://example.com/form
- button "提交" [ref=e10]
- button "确定" [ref=e11]
- textbox "用户名" [ref=e12]
- combobox "单位" [ref=e13]
- option "汉东省院" [ref=e14]
"""


def test_extract_label_hints():
    assert extract_label_hints("点击【提交】") == ["提交"]
    assert "单位" in extract_label_hints("在「单位」中选择【汉东省院】")


def test_match_unique_button():
    intent = StepIntent(action="click", target_role="button", target_name="提交")
    cands = match_intent_candidates(SNAP, intent)
    assert len(cands) == 1
    assert cands[0]["ref"] == "e10"


def test_match_ambiguous_without_role_still_unique_by_name():
    intent = StepIntent(action="click", target_role=None, target_name="确定")
    cands = match_intent_candidates(SNAP, intent)
    assert len(cands) == 1
    assert cands[0]["ref"] == "e11"


def test_intent_to_tool_call_binds_ref():
    intent = StepIntent(action="click", target_role="button", target_name="提交", thinking="ok")
    tc = intent_to_tool_call(intent, ref="e10")
    assert tc.action == "click"
    assert tc.selector == "e10"


def test_intent_to_tool_call_wait_no_ref():
    intent = StepIntent(action="wait", value="加载完成")
    tc = intent_to_tool_call(intent, ref=None)
    assert tc.action == "wait"
    assert tc.value == "加载完成"


def test_plan_blob_roundtrip():
    blob = build_plan_blob(
        [
            {"action": "click", "role": "combobox", "name": "单位"},
            {"action": "click", "role": "option", "name": "汉东省院"},
        ],
        page_url_hint="/form",
    )
    assert blob["version"] == 2
    steps = get_plan_steps(blob)
    assert len(steps) == 2
    assert steps[0]["name"] == "单位"


def test_v1_fingerprint_as_one_step_plan():
    steps = get_plan_steps({"action": "click", "role": "button", "name": "提交"})
    assert len(steps) == 1
    assert steps[0]["name"] == "提交"


def test_should_verify_forces_when_expected():
    assert VerificationStrategy.should_verify("fill", None, has_expected=True) is True
    assert VerificationStrategy.should_verify("fill", None, has_expected=False) is False


def test_try_replay_plan_mcp_unique_then_fail():
    import asyncio
    from core.locator_memory import try_replay_plan_mcp

    snap_open = """
Page URL: https://example.com/form
- combobox "单位" [ref=e1]
"""
    snap_open_opts = """
Page URL: https://example.com/form
- combobox "单位" [ref=e1]
- option "汉东省院" [ref=e2]
"""

    class _M:
        async def execute_tool_call(self, tc):
            return {"success": True}

        async def get_dom_snapshot(self):
            return snap_open_opts

    blob = build_plan_blob(
        [
            {"action": "click", "role": "combobox", "name": "单位"},
            {"action": "click", "role": "option", "name": "汉东省院"},
        ],
        page_url_hint="/form",
    )
    ok = asyncio.run(
        try_replay_plan_mcp(_M(), blob, snapshot=snap_open, step_description="选择【汉东省院】"),
    )
    assert ok["success"] and ok["plan_replay"]
    assert ok["tool_call"]["selector"] == "e2"

    bad = build_plan_blob(
        [{"action": "click", "role": "button", "name": "不存在"}],
        page_url_hint="/form",
    )
    fail = asyncio.run(
        try_replay_plan_mcp(_M(), bad, snapshot=snap_open, step_description="点【不存在】"),
    )
    assert not fail["success"]
