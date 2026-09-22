#!/usr/bin/env python3
"""Local verification of OTA Cursor alignment (mocked MCP / LLM)."""
from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, r"D:\uitest\VoyanTest")

from core.ota_cursor import (
    OTA_CURSOR_SYSTEM_PROMPT,
    should_capture_screenshot,
    parse_bbox_center_from_text,
    click_xy_fallback_after_ref_fail,
    maybe_rewrite_click_xy_from_ref,
    bridge_evaluate_act,
    bridge_click_xy_act,
)
from core.locator_candidates import LocatorCandidate
from core.agent_runner.runner import AgentRunner


PASSED = 0
FAILED = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"[PASS] {name}")
    else:
        FAILED += 1
        print(f"[FAIL] {name}" + (f" — {detail}" if detail else ""))


def test_helpers() -> None:
    check("prompt has click_xy", "click_xy" in OTA_CURSOR_SYSTEM_PROMPT)
    check("prompt has upload", "upload" in OTA_CURSOR_SYSTEM_PROMPT)
    check("overlay needs shot", should_capture_screenshot("role=dialog [ref=e1]"))
    check("plain no shot", not should_capture_screenshot("button 'OK' [ref=e2]"))
    check("fail needs shot", should_capture_screenshot("ok", consecutive_failures=1))
    check("canvas needs shot", should_capture_screenshot("canvas widget"))
    check("parse bbox", parse_bbox_center_from_text('{"x":12,"y":34}') == (12, 34))
    check("bridge eval act", bridge_evaluate_act("button", "确定") is not None)
    check("bridge xy act", bridge_click_xy_act(5, 6)["args"]["value"] == "5,6")


async def test_runner_observe_and_fallback() -> None:
    mcp = MagicMock()
    mcp.get_dom_snapshot = AsyncMock(
        return_value="- button 'Submit' [ref=e9]\n[LOCATOR_STATE] visible_overlay=false"
    )
    mcp.call_tool = AsyncMock(
        side_effect=lambda tool, args: {
            "success": True,
            "text": '{"x":100,"y":200,"w":10,"h":10}'
            if tool == "browser_evaluate" and "getBoundingClientRect" in str(args)
            or (tool == "browser_evaluate" and "role" in str(args))
            else {"success": True, "text": "https://example.com"},
        }
    )
    mcp.take_screenshot = AsyncMock(return_value=None)
    mcp.execute_tool_call = AsyncMock(return_value={"success": True})

    # Fix call_tool mock more carefully
    async def _call_tool(tool, args=None):
        args = args or {}
        fn = str(args.get("function") or "")
        if "location.href" in fn:
            return {"success": True, "text": "https://example.com"}
        if "getBoundingClientRect" in fn or "roleSel" in fn:
            return {"success": True, "text": '{"x":100,"y":200,"w":10,"h":10}'}
        return {"success": True, "text": ""}

    mcp.call_tool = AsyncMock(side_effect=_call_tool)

    runner = AgentRunner(
        mcp_manager=mcp,
        goal="click submit",
        llm_client=MagicMock(),
        max_turns=3,
    )
    runner._consecutive_act_failures = 0
    obs = await runner.observe()
    check("observe has candidates", len(obs.get("candidates") or ()) >= 1)
    check("observe no shot on plain", obs.get("screenshot_b64") is None)

    runner._consecutive_act_failures = 1
    # Patch the symbol already bound into runner module
    import core.agent_runner.runner as runner_mod

    async def _fake_shot(_mcp):
        return "AAAA"

    orig_cap = runner_mod.capture_screenshot_b64
    runner_mod.capture_screenshot_b64 = _fake_shot  # type: ignore
    try:
        obs2 = await runner.observe(force_screenshot=True)
    finally:
        runner_mod.capture_screenshot_b64 = orig_cap  # type: ignore
    check("observe shot when forced", obs2.get("screenshot_b64") == "AAAA")

    cands = (
        LocatorCandidate(
            ref="e9",
            role="button",
            name="Submit",
            text="Submit",
            attributes={},
            frame_path=(),
        ),
    )
    failed = {"action": "click", "selector": "e9", "value": None}
    xy = await click_xy_fallback_after_ref_fail(
        failed, mcp_manager=mcp, candidates=cands
    )
    check("click_xy fallback built", xy is not None and xy.get("action") == "click_xy")
    check("click_xy value coords", (xy or {}).get("value") == "100,200")

    rewritten = await maybe_rewrite_click_xy_from_ref(
        {"action": "click_xy", "selector": "e9", "value": None},
        mcp_manager=mcp,
        candidates=cands,
    )
    check(
        "rewrite click_xy from ref",
        rewritten.get("selector") is None and rewritten.get("value") == "100,200",
    )


async def test_think_passes_vision_flags() -> None:
    """think() must call generate_tool_call with screenshot + replace_system_prompt."""
    import core.agent_runner.runner as runner_mod

    calls = {}

    async def fake_gen(**kwargs):
        calls.update(kwargs)
        return SimpleNamespace(
            model_dump=lambda: {
                "action": "click",
                "selector": "e1",
                "value": None,
                "thinking": "ok",
            }
        )

    mcp = MagicMock()
    mcp.execute_tool_call = AsyncMock(return_value={"success": True})
    runner = AgentRunner(mcp, "goal", MagicMock(), max_turns=2)
    runner.context.get_context = lambda: "(empty)"

    # Patch generate_tool_call used inside think
    import core.llm_wrapper as lw

    orig = lw.generate_tool_call
    lw.generate_tool_call = fake_gen  # type: ignore
    try:
        # Also need validate to pass — use non-ref selector to skip validation
        async def fake_gen2(**kwargs):
            calls.update(kwargs)
            return SimpleNamespace(
                model_dump=lambda: {
                    "action": "goto",
                    "selector": None,
                    "value": "https://example.com",
                    "thinking": "nav",
                }
            )

        lw.generate_tool_call = fake_gen2  # type: ignore
        await runner.think(
            {
                "snapshot": "page",
                "url": "about:blank",
                "screenshot_b64": "IMG",
                "snapshot_version": "abc",
                "candidates": (),
            }
        )
    finally:
        lw.generate_tool_call = orig  # type: ignore

    check("think passes screenshot_b64", calls.get("screenshot_b64") == "IMG")
    check("think replace_system_prompt", calls.get("replace_system_prompt") is True)
    check(
        "think system is cursor prompt",
        "click_xy" in (calls.get("system_prompt") or ""),
    )


async def main() -> int:
    test_helpers()
    await test_runner_observe_and_fallback()
    await test_think_passes_vision_flags()
    print(f"\n=== {PASSED} passed, {FAILED} failed ===")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
