# tests/test_nl_goal_smoke.py
"""Smoke tests for nl_goal plumbing (no live browser/LLM)."""
from core.goal_agent_loop import (
    GoalAction,
    build_goal_text,
    detect_stagnation,
    journal_entry,
    steps_results_from_goal,
    tool_call_from_decision,
)
from app.runtime_config import normalize_execution_backend, ExecutionBackendConfig
from core.script_synthesize import _ensure_entrypoint


def test_normalize_backends():
    assert normalize_execution_backend("hybrid") == "legacy_hybrid"
    assert normalize_execution_backend("playwright_mcp") == "legacy_mcp"
    assert normalize_execution_backend("nl_goal") == "nl_goal"
    assert normalize_execution_backend(None) == "nl_goal"
    cfg = ExecutionBackendConfig()
    assert cfg.backend == "nl_goal"
    assert cfg.dry_run_mode == "skip"


def test_dry_run_mode_coerce():
    assert ExecutionBackendConfig(dry_run_mode="isolated").dry_run_mode == "isolated"
    assert ExecutionBackendConfig(dry_run_mode="attach").dry_run_mode == "attach"
    assert ExecutionBackendConfig(dry_run_mode="bogus").dry_run_mode == "skip"
    assert ExecutionBackendConfig(dry_run_mode=None).dry_run_mode == "skip"


def test_build_goal_soft_checklist():
    text = build_goal_text(
        case_name="登录",
        description="选单位并登录",
        steps=[
            {
                "step_order": 1,
                "description": "选择京州市院",
                "structured_step": {"selector": "input[placeholder='请选择单位']"},
            },
            {"step_order": 2, "description": "输入用户名", "expected_result": "已填"},
        ],
    )
    assert "京州市院" in text
    assert "CHECKLIST" in text
    assert "selector_hint" in text


def test_tool_call_and_stagnation():
    d = GoalAction(action="fill", selector="e2", value="test")
    tc = tool_call_from_decision(d)
    assert tc["action"] == "fill"
    assert tc["value"] == "test"
    journal = [
        journal_entry(turn=i, decision=d, success=False, error="x")
        for i in range(5)
    ]
    assert detect_stagnation(journal)


def test_steps_results_and_entrypoint():
    steps = [{"step_order": 1, "description": "a"}, {"step_order": 2, "description": "b"}]
    # success=True with no checklist coverage must NOT fake-pass
    rows = steps_results_from_goal(steps, success=True, journal=[{"turn": 1}])
    assert len(rows) == 2
    assert all(r["status"] == "failed" and not r["success"] for r in rows)
    assert "was not executed" in (rows[0]["error"] or "")
    assert rows[0].get("action_journal")
    # full coverage → all passed
    covered = [
        {"checklist_index": 1, "success": True},
        {"checklist_index": 2, "success": True},
    ]
    rows2 = steps_results_from_goal(steps, success=True, journal=covered)
    assert all(r["status"] == "passed" and r["success"] for r in rows2)
    src = _ensure_entrypoint("async def test_case_x(page):\n    await page.goto('/')\n", 9)
    assert "test_case_9" in src


def _ten_steps():
    return [{"step_order": i, "description": f"step{i}"} for i in range(1, 11)]


def test_steps_results_fail_at_9_with_shot():
    """Covered 5–7, unrecovered fail at 9 → uncovered before 9 failed, 9 failed+shot, 10 skipped."""
    shot = "reports/run_x/screenshots/nl_goal_step_9_turn_12.png"
    journal = [
        {"checklist_index": 5, "success": True},
        {"checklist_index": 6, "success": True},
        {"checklist_index": 7, "success": True},
        {
            "checklist_index": 9,
            "success": False,
            "error": "click missed",
            "screenshot_path": shot,
        },
    ]
    rows = steps_results_from_goal(
        _ten_steps(), success=False, journal=journal, error="click missed"
    )
    assert [r["status"] for r in rows] == (
        ["failed"] * 4 + ["passed"] * 3 + ["failed", "failed", "skipped"]
    )
    # Uncovered steps 1–4 / 8 must not be fake-passed
    assert rows[0]["error"] == "nl_goal step 1 was not executed"
    assert rows[7]["error"] == "nl_goal step 8 was not executed"
    assert rows[8]["step_number"] == 9
    assert rows[8]["screenshot_path"] == shot
    assert rows[8]["error"] == "click missed"
    assert rows[9]["status"] == "skipped"
    assert rows[9]["screenshot_path"] is None


def test_steps_results_max_turns_after_cover_9():
    """Progress through 9 then max_turns → 1–9 passed, 10 failed (shot from journal)."""
    shot = "reports/run_x/screenshots/nl_goal_final_fail.png"
    journal = [
        {"checklist_index": i, "success": True} for i in range(1, 10)
    ] + [
        {
            "checklist_index": None,
            "success": False,
            "error": "nl_goal hit max_turns=40",
            "screenshot_path": shot,
        }
    ]
    rows = steps_results_from_goal(
        _ten_steps(),
        success=False,
        journal=journal,
        error="nl_goal hit max_turns=40",
    )
    assert [r["status"] for r in rows] == ["passed"] * 9 + ["failed"]
    assert rows[9]["step_number"] == 10
    assert rows[9]["screenshot_path"] == shot


def test_steps_results_gap_uncovered_gets_final_shot():
    """covered={1..7,9} max_turns → fail step 8 (first gap) with final screenshot."""
    shot = "reports/run_x/screenshots/nl_goal_final_fail.png"
    journal = [
        {"checklist_index": i, "success": True, "action": "click"}
        for i in (1, 2, 3, 4, 5, 6, 7, 9)
    ] + [
        {
            "checklist_index": None,
            "success": False,
            "error": "nl_goal hit max_turns=40",
            "screenshot_path": shot,
            "screenshot_on_fail": True,
        }
    ]
    rows = steps_results_from_goal(
        _ten_steps(),
        success=False,
        journal=journal,
        error="nl_goal hit max_turns=40",
    )
    assert rows[7]["step_number"] == 8
    assert rows[7]["status"] == "failed"
    assert rows[7]["screenshot_path"] == shot
    assert rows[8]["status"] == "skipped"  # 9 was covered but after fail cursor
    assert rows[9]["status"] == "skipped"


def test_fill_cannot_cover_click_checklist_step():
    """Successful fill/search must not mark a 点击 checklist item as passed."""
    from core.goal_agent_loop import (
        covered_checklist_indices,
        journal_entry_covers_checklist,
    )

    steps = [
        {"step_order": 1, "description": "点击【问题反馈测试LJ072301】"},
        {"step_order": 2, "description": "点击【功能】"},
        {"step_order": 3, "description": "在【4888智能辅助】输入 4888智能辅助"},
    ]
    fill_entry = {
        "checklist_index": 1,
        "success": True,
        "action": "fill",
        "value": "问题反馈测试LJ072301",
    }
    assert journal_entry_covers_checklist(
        fill_entry, step_description=steps[0]["description"]
    ) is False
    journal = [
        fill_entry,
        {
            "checklist_index": 2,
            "success": True,
            "action": "fill",
            "value": "",
        },
        {
            "checklist_index": 3,
            "success": True,
            "action": "fill",
            "value": "4888智能辅助",
        },
        {
            "checklist_index": 1,
            "success": False,
            "action": "evaluate",
            "error": "SyntaxError",
            "screenshot_path": "shot1.png",
        },
    ]
    # Fail at 1 unrecovered (fill does not cover click) → step1 failed, rest skipped
    assert covered_checklist_indices(journal, steps) == {3}
    rows = steps_results_from_goal(steps, success=False, journal=journal)
    assert rows[0]["status"] == "failed"
    assert rows[0]["success"] is False
    assert rows[1]["status"] == "skipped"
    assert rows[2]["status"] == "skipped"


def test_batch25_style_uncovered_before_fail_not_passed():
    """Batch25 run28 pattern: wrong fills tagged as click steps must not pass 1–5."""
    steps = [
        {"step_order": 1, "description": "点击【问题反馈测试LJ072301】"},
        {"step_order": 2, "description": "点击【功能】"},
        {"step_order": 3, "description": "在【4888智能辅助】输入 4888智能辅助"},
        {"step_order": 4, "description": "点击【4888智能辅助（高检】"},
        {"step_order": 5, "description": "点击右上角书本形状图标（用途：打开菜单）"},
        {"step_order": 6, "description": "点击【提交问题反馈】"},
        {"step_order": 7, "description": "点击【提交反馈】"},
    ]
    journal = [
        {
            "checklist_index": 1,
            "success": False,
            "action": "evaluate",
            "error": "SyntaxError",
            "screenshot_path": "s1.png",
        },
        {"checklist_index": 1, "success": True, "action": "fill", "value": "问题反馈"},
        {"checklist_index": 3, "success": True, "action": "click", "selector": "e1"},
        {"checklist_index": 2, "success": True, "action": "fill", "value": ""},
        {"checklist_index": 3, "success": True, "action": "fill", "value": "4888智能辅助"},
        {"checklist_index": 4, "success": True, "action": "click", "selector": "e2"},
        {
            "checklist_index": 5,
            "success": True,
            "action": "evaluate",
            "value": "const x=document.querySelector('x'); if(x){x.click();return true} return false;",
        },
        {
            "checklist_index": 6,
            "success": False,
            "action": "evaluate",
            "error": "invalid selector",
            "screenshot_path": "s6.png",
        },
    ]
    rows = steps_results_from_goal(
        steps, success=False, journal=journal, error="invalid selector"
    )
    # Earliest unrecovered fail is step 1 (fill cannot cover click)
    assert rows[0]["status"] == "failed"
    assert "SyntaxError" in (rows[0]["error"] or "")
    assert all(r["status"] == "skipped" for r in rows[1:])
    assert not any(r["status"] == "passed" for r in rows)


def test_steps_results_success_all_covered_passed():
    journal = [{"checklist_index": i, "success": True} for i in range(1, 11)]
    rows = steps_results_from_goal(_ten_steps(), success=True, journal=journal)
    assert all(r["status"] == "passed" and r["success"] for r in rows)
    assert all(r["screenshot_path"] is None for r in rows)


def test_steps_results_done_uncovered_close_step_failed():
    """success=True but journal only covers through 8 → step 9 (关关闭) must failed."""
    journal = [
        {"checklist_index": i, "success": True, "action": "click"}
        for i in range(1, 9)
    ]
    steps = _ten_steps()
    steps[8]["description"] = "点击（页面所有出现的消息的关闭按钮）"
    rows = steps_results_from_goal(steps, success=True, journal=journal)
    assert [r["status"] for r in rows[:8]] == ["passed"] * 8
    assert rows[8]["status"] == "failed"
    assert rows[8]["success"] is False
    assert rows[8]["error"] == "nl_goal marked done but step 9 was not executed"
    assert rows[9]["status"] == "failed"
    assert rows[9]["error"] == "nl_goal marked done but step 10 was not executed"
    assert all(r["screenshot_path"] is None for r in rows)


def test_evaluate_cannot_cover_close_messages_step():
    """Failed close click + evaluate 'success' must not fake-pass the close step."""
    from core.goal_agent_loop import (
        covered_checklist_indices,
        uncovered_checklist_orders,
    )

    steps = [
        {"step_order": 1, "description": "点击【登录】"},
        {"step_order": 2, "description": "点击（页面所有出现的消息的关闭按钮）"},
    ]
    journal = [
        {"checklist_index": 1, "success": True, "action": "click"},
        {
            "checklist_index": 2,
            "success": False,
            "action": "click",
            "error": "timeout",
        },
        {
            "checklist_index": 2,
            "success": True,
            "action": "evaluate",
            "checklist_note": "Verifying no dialogs",
        },
    ]
    assert covered_checklist_indices(journal, steps) == {1}
    assert uncovered_checklist_orders(steps, journal) == [2]
    rows = steps_results_from_goal(steps, success=True, journal=journal)
    assert rows[0]["status"] == "passed"
    assert rows[1]["status"] == "failed"
    assert rows[1]["success"] is False


def test_cursor_style_close_evaluate_covers_close_step():
    """Evaluate that actually clicks dialog/notification close counts as cover."""
    from core.goal_agent_loop import (
        CLOSE_ALL_PAGE_PROMPTS_JS,
        covered_checklist_indices,
        uncovered_checklist_orders,
    )

    steps = [
        {"step_order": 1, "description": "点击【登录】"},
        {"step_order": 2, "description": "点击（页面所有出现的消息的关闭按钮）"},
    ]
    journal = [
        {"checklist_index": 1, "success": True, "action": "click"},
        {
            "checklist_index": 2,
            "success": True,
            "action": "evaluate",
            "value": CLOSE_ALL_PAGE_PROMPTS_JS,
            "checklist_note": "CLOSE_ALL_PAGE_PROMPTS checklist item 2",
            "stable_hint": "CLOSE_ALL_PAGE_PROMPTS",
            "result_snippet": '{"ok": true, "clicked": ["dialog:footer"]}',
        },
    ]
    assert covered_checklist_indices(journal, steps) == {1, 2}
    assert uncovered_checklist_orders(steps, journal) == []


def test_seed_open_steps_after_navigation():
    from core.goal_agent_loop import seed_open_steps_after_navigation

    steps = [
        {"step_order": 1, "description": "点击【登录】"},
        {"step_order": 2, "description": "打开【xtmh】"},
    ]
    seeded = seed_open_steps_after_navigation(steps, "http://192.168.9.125/xtmh")
    assert len(seeded) == 1
    assert seeded[0]["checklist_index"] == 2
    assert seeded[0]["action"] == "goto"
    assert seeded[0]["success"] is True


import asyncio
from unittest.mock import AsyncMock, MagicMock, patch


def _nl_goal_session_patches(mgr, *, snap_return=None):
    from core.goal_agent_loop import uncovered_checklist_orders

    session = MagicMock()
    session.agent = MagicMock()
    session.agent.status = MagicMock()
    session.agent.capabilities = ["browser_use", "playwright_mcp"]
    session.request = AsyncMock(return_value={"ready": True})
    session.send = AsyncMock()
    snap = AsyncMock(return_value=snap_return if snap_return is not None else [])

    async def _decide_cover_then_done(**kwargs):
        steps = kwargs.get("steps")
        journal_tail = kwargs.get("journal_tail") or []
        uncovered = uncovered_checklist_orders(steps, journal_tail)
        if uncovered:
            idx = uncovered[0]
            return GoalAction(
                status="continue",
                action="click",
                selector="e1",
                checklist_index=idx,
                checklist_note=f"checklist item {idx}",
                thinking=f"cover {idx}",
            )
        return GoalAction(status="done", thinking="all good")

    return (
        session,
        [
            patch.object(mgr, "get_session", new=AsyncMock(return_value=session)),
            patch.object(mgr, "_get_snapshot", new=AsyncMock(return_value={"ok": True})),
            patch.object(
                mgr,
                "_snapshot_indicates_browser_closed",
                return_value=False,
            ),
            patch(
                "agent.manager.create_openai_client",
                new=AsyncMock(return_value=MagicMock()),
            ),
            patch(
                "agent.manager._llm_resolve_config",
                new=AsyncMock(return_value=(None, None, "fake-model")),
            ),
            patch(
                "core.goal_agent_loop.decide_next_goal_action",
                new=AsyncMock(side_effect=_decide_cover_then_done),
            ),
            patch.object(mgr, "_execute_on_agent_snapshot_path", new=snap),
            patch.object(
                mgr,
                "_execute_step",
                new=AsyncMock(return_value={"success": True, "text": "ok"}),
            ),
        ],
        snap,
    )


def test_nl_goal_default_skip_verifies_before_persist():
    """skip: goal DONE → synthesize → headless verify → persist only if verify ok."""
    from agent.manager import AgentManager
    from app.runtime_config import execution_backend_config

    mgr = AgentManager()
    steps = [
        {"step_order": 1, "description": "a"},
        {"step_order": 2, "description": "b"},
    ]
    prev = execution_backend_config.dry_run_mode
    execution_backend_config.dry_run_mode = "skip"

    async def _run():
        _session, patches, snap = _nl_goal_session_patches(mgr)
        try_run = AsyncMock(
            return_value=[
                {"success": True, "step_number": 1},
                {"success": True, "step_number": 2},
            ]
        )
        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
            patches[7],
            patch(
                "core.script_synthesize.synthesize_playwright_script",
                new=AsyncMock(return_value="async def test_case_42(page):\n    pass\n"),
            ),
            patch.object(mgr, "_try_run_compiled_script", new=try_run),
        ):
            out = await mgr._execute_on_agent_nl_goal(
                "agent-1",
                "run-skip",
                "case",
                steps,
                case_id=42,
                navigate_base_url=False,
            )
            try_run.assert_awaited()
            snap.assert_not_awaited()
            assert isinstance(out, list) and len(out) == 2
            assert all(r.get("success") for r in out)
            assert all(r.get("backend") == "nl_goal" for r in out)
            assert out[0].get("action_journal")
            synth = mgr._last_synthesized_script
            assert synth and "test_case_42" in (synth.get("script") or "")

    try:
        asyncio.run(_run())
    finally:
        execution_backend_config.dry_run_mode = prev


def test_nl_goal_skip_verify_fail_does_not_persist():
    """skip + verify fail → case still passed, script not persisted."""
    from agent.manager import AgentManager
    from app.runtime_config import execution_backend_config

    mgr = AgentManager()
    steps = [
        {"step_order": 1, "description": "a"},
        {"step_order": 2, "description": "b"},
    ]
    prev = execution_backend_config.dry_run_mode
    execution_backend_config.dry_run_mode = "skip"

    async def _run():
        _session, patches, snap = _nl_goal_session_patches(mgr)
        try_run = AsyncMock(
            return_value=[
                {
                    "success": False,
                    "compiled_script_failed": True,
                    "error": "boom",
                }
            ]
        )
        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
            patches[7],
            patch(
                "core.script_synthesize.synthesize_playwright_script",
                new=AsyncMock(return_value="async def test_case_42(page):\n    pass\n"),
            ),
            patch(
                "core.script_synthesize.repair_playwright_script",
                new=AsyncMock(return_value="async def test_case_42(page):\n    pass\n"),
            ),
            patch.object(mgr, "_try_run_compiled_script", new=try_run),
        ):
            out = await mgr._execute_on_agent_nl_goal(
                "agent-1",
                "run-skip-fail",
                "case",
                steps,
                case_id=42,
                navigate_base_url=False,
            )
            assert all(r.get("success") for r in out)
            assert mgr._last_synthesized_script is None

    try:
        asyncio.run(_run())
    finally:
        execution_backend_config.dry_run_mode = prev


def test_nl_goal_script_verify_unlocated_keeps_goal_no_hybrid():
    """isolated + synthesize fail (unlocated) → keep goal; no whole-case/last-step hybrid."""
    from agent.manager import AgentManager
    from app.runtime_config import execution_backend_config

    mgr = AgentManager()
    steps = [
        {"step_order": 1, "description": "a"},
        {"step_order": 2, "description": "b"},
    ]
    prev = execution_backend_config.dry_run_mode
    execution_backend_config.dry_run_mode = "isolated"

    async def _run():
        _session, patches, snap = _nl_goal_session_patches(mgr)
        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
            patches[7],
            patch(
                "core.script_synthesize.synthesize_playwright_script",
                new=AsyncMock(side_effect=RuntimeError("synth boom")),
            ),
        ):
            out = await mgr._execute_on_agent_nl_goal(
                "agent-1",
                "run-1",
                "case",
                steps,
                case_id=42,
                navigate_base_url=False,
            )
            snap.assert_not_awaited()
            assert isinstance(out, list) and len(out) == 2
            assert all(r.get("success") for r in out)
            assert all(r.get("backend") == "nl_goal" for r in out)
            assert out[0].get("nl_goal_script_fallback_attempted") is True
            assert out[0].get("nl_goal_script_verify_unlocated") is True
            assert not any(r.get("nl_goal_script_fallback") for r in out)
            assert out[0].get("action_journal")

    try:
        asyncio.run(_run())
    finally:
        execution_backend_config.dry_run_mode = prev


def test_nl_goal_script_verify_located_step_hybrid_only():
    """isolated + located dry-run failure → hybrid NL for that step only; merge into goal rows."""
    from agent.manager import AgentManager
    from app.runtime_config import execution_backend_config

    mgr = AgentManager()
    steps = [
        {"step_order": 1, "description": "a"},
        {"step_order": 2, "description": "b"},
        {"step_order": 3, "description": "c"},
    ]
    hybrid_step2 = [
        {
            "step_number": 2,
            "original_description": "b",
            "success": True,
            "status": "passed",
            "backend": "hybrid",
            "resolved_selector": "#ok",
        },
    ]
    prev = execution_backend_config.dry_run_mode
    execution_backend_config.dry_run_mode = "isolated"

    async def _run():
        _session, patches, snap = _nl_goal_session_patches(
            mgr, snap_return=hybrid_step2
        )
        dry_rows = [
            {"step_number": 1, "success": True},
            {"step_number": 2, "success": False, "error": "boom at step 2"},
            {"step_number": 3, "success": False, "status": "skipped"},
        ]
        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
            patches[7],
            patch(
                "core.script_synthesize.synthesize_playwright_script",
                new=AsyncMock(return_value="async def test_case_42(page):\n    pass\n"),
            ),
            patch(
                "core.script_synthesize.repair_playwright_script",
                new=AsyncMock(return_value="async def test_case_42(page):\n    pass\n"),
            ),
            patch.object(
                mgr,
                "_try_run_compiled_script",
                new=AsyncMock(return_value=dry_rows),
            ),
            patch(
                "core.compiled_script.build_script_from_run",
                return_value=None,
            ),
        ):
            out = await mgr._execute_on_agent_nl_goal(
                "agent-1",
                "run-1",
                "case",
                steps,
                case_id=42,
                navigate_base_url=False,
            )
            snap.assert_awaited_once()
            call_kwargs = snap.await_args.kwargs
            call_args = snap.await_args.args
            fb_steps = call_kwargs.get("steps")
            if fb_steps is None and len(call_args) >= 4:
                fb_steps = call_args[3]
            assert fb_steps == [steps[1]]
            assert call_kwargs.get("navigate_base_url") is False
            assert call_kwargs.get("reuse_existing_browser") is True
            assert call_kwargs.get("selected") == "hybrid" or (
                call_args and "hybrid" in str(snap.await_args)
            )
            start_msg = _session.request.await_args_list[0].args[0]
            assert start_msg.payload.get("backend") == "hybrid"
            assert isinstance(out, list) and len(out) == 3
            assert out[0].get("backend") == "nl_goal"
            assert out[0].get("nl_goal_script_fallback") is not True
            assert out[1].get("nl_goal_script_fallback") is True
            assert out[1].get("step_number") == 2
            assert out[2].get("backend") == "nl_goal"
            assert out[0].get("action_journal")

    try:
        asyncio.run(_run())
    finally:
        execution_backend_config.dry_run_mode = prev


def test_nl_goal_script_fallback_keeps_goal_when_hybrid_fails():
    """isolated + located step hybrid fails → keep goal success results (do not flip case)."""
    from agent.manager import AgentManager
    from app.runtime_config import execution_backend_config

    mgr = AgentManager()
    steps = [
        {"step_order": 1, "description": "a"},
        {"step_order": 2, "description": "b"},
    ]
    hybrid_fail = [
        {
            "step_number": 2,
            "original_description": "b",
            "success": False,
            "status": "failed",
            "error": "Expected result verification failed",
            "backend": "hybrid",
        },
    ]
    prev = execution_backend_config.dry_run_mode
    execution_backend_config.dry_run_mode = "isolated"

    async def _run():
        _session, patches, snap = _nl_goal_session_patches(
            mgr, snap_return=hybrid_fail
        )
        dry_rows = [
            {"step_number": 1, "success": True},
            {"step_number": 2, "success": False, "error": "boom"},
        ]
        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
            patches[7],
            patch(
                "core.script_synthesize.synthesize_playwright_script",
                new=AsyncMock(return_value="async def test_case_42(page):\n    pass\n"),
            ),
            patch(
                "core.script_synthesize.repair_playwright_script",
                new=AsyncMock(return_value="async def test_case_42(page):\n    pass\n"),
            ),
            patch.object(
                mgr,
                "_try_run_compiled_script",
                new=AsyncMock(side_effect=[dry_rows, dry_rows]),
            ),
        ):
            out = await mgr._execute_on_agent_nl_goal(
                "agent-1",
                "run-2",
                "case",
                steps,
                case_id=42,
                navigate_base_url=False,
            )
            snap.assert_awaited_once()
            fb_steps = snap.await_args.kwargs.get("steps")
            if fb_steps is None and len(snap.await_args.args) >= 4:
                fb_steps = snap.await_args.args[3]
            assert fb_steps == [steps[1]]
            assert snap.await_args.kwargs.get("navigate_base_url") is False
            assert snap.await_args.kwargs.get("reuse_existing_browser") is True
            assert all(r.get("success") for r in out)
            assert all(r.get("backend") == "nl_goal" for r in out)
            assert out[0].get("nl_goal_script_fallback_attempted") is True
            assert not any(r.get("nl_goal_script_fallback") for r in out)

    try:
        asyncio.run(_run())
    finally:
        execution_backend_config.dry_run_mode = prev


def test_pick_nl_script_fallback_step_unlocated_is_none():
    from agent.manager import _pick_nl_script_fallback_step

    steps = [
        {"step_order": 1, "description": "a"},
        {"step_order": 2, "description": "b"},
        {"step_order": 3, "description": "c"},
    ]
    # Whole-script compiled_script_failed blob with step_number=1 must not pin step 1
    assert (
        _pick_nl_script_fallback_step(
            steps,
            dry_verify=[
                {
                    "step_number": 1,
                    "success": False,
                    "compiled_script_failed": True,
                    "error": "Timeout",
                }
            ],
            dry_error="Timeout",
        )
        is None
    )

    s2, order2 = _pick_nl_script_fallback_step(
        steps,
        dry_verify=[
            {"step_number": 1, "success": True},
            {"step_number": 2, "success": False, "error": "boom"},
            {"step_number": 3, "success": False, "status": "skipped"},
        ],
        dry_error="boom",
    )
    assert order2 == 2
    assert s2 is steps[1]

    s3, order3 = _pick_nl_script_fallback_step(
        steps, dry_verify=None, dry_error="checklist step 1 failed"
    )
    assert order3 == 1
    assert s3 is steps[0]


# ---------------------------------------------------------------------------
# DOM probe（失败恢复）相关
# ---------------------------------------------------------------------------

def test_extract_probe_keywords_mixed():
    from core.dom_probe import extract_probe_keywords

    # 中英混合：实体词保留、动词壳去除
    kws = extract_probe_keywords("点击【京州市院】输入用户名", None)
    assert "京州市院" in kws
    assert "用户名" in kws
    assert "点击" not in kws

    # placeholder= 值保留
    kws2 = extract_probe_keywords("选择单位", "placeholder=请选择单位")
    assert "请选择单位" in kws2

    # role=button name=登录 解析
    kws3 = extract_probe_keywords("登录", "role=button name=登录")
    assert "button" in kws3
    assert "登录" in kws3

    # text= 解析
    kws4 = extract_probe_keywords("点确认", "text=确定")
    assert "确定" in kws4

    # 关闭/确定 是实体词，必须保留
    kws5 = extract_probe_keywords("点击（页面所有出现的消息的关闭按钮）", None)
    assert any("关闭" in k for k in kws5)

    # 纯动词壳 → 无关键词
    assert extract_probe_keywords("点击", None) == []


def test_build_probe_js_serializes_keywords():
    from core.dom_probe import GENERIC_DOM_PROBE_JS, build_probe_js

    js = build_probe_js(["确定", "京州市院"])
    assert js.startswith("() =>")
    assert "确定" in js
    assert "京州市院" in js
    assert "__PROBE_KEYWORDS__" not in js
    # 常量本身保留占位符，可重复注入
    assert "__PROBE_KEYWORDS__" in GENERIC_DOM_PROBE_JS


def test_build_probe_summary_compact():
    from core.dom_probe import build_probe_summary

    result = {
        "ok": True,
        "keywords": ["确定"],
        "candidates": [
            {
                "tag": "button", "role": "", "name": "", "text": "确定",
                "placeholder": "", "visible": True, "candidate_index": 0,
                "dom_index": 3, "locator": "probe_idx_3",
            },
            {
                "tag": "input", "role": "", "name": "", "text": "",
                "placeholder": "请选择单位", "visible": True,
                "candidate_index": 1, "dom_index": 7, "locator": "probe_idx_7",
            },
        ],
    }
    text = build_probe_summary(result)
    assert "DOM PROBE found 2 visible clickable candidate(s)" in text
    assert "probe_idx_3" in text
    assert "确定" in text
    assert "请选择单位" in text
    assert "SUGGESTED evaluate" in text

    empty = build_probe_summary({"ok": True, "keywords": ["确定"], "candidates": []})
    assert "DOM PROBE found 0" in empty
    assert "No visible clickable candidate" in empty
    assert build_probe_summary(None)


def test_decide_next_goal_action_probe_injected():
    import asyncio

    from core.goal_agent_loop import decide_next_goal_action

    seen = {}

    def _fake_response(*args, **kwargs):
        seen["messages"] = kwargs.get("messages")
        return type(
            "Resp", (),
            {
                "choices": [
                    type(
                        "C", (),
                        {
                            "message": type(
                                "M", (),
                                {
                                    "content": (
                                        '{"status":"continue","action":"click",'
                                        '"selector":"probe_idx_3","checklist_index":1}'
                                    )
                                },
                            )()
                        },
                    )()
                ]
            },
        )()

    client = MagicMock()
    client.chat.completions.create = AsyncMock(side_effect=_fake_response)

    async def _run():
        await decide_next_goal_action(
            client=client,
            model="fake-model",
            goal_text="GOAL",
            snapshot="SNAP",
            journal_tail=[],
            probe_result=(
                "DOM PROBE found 1 visible clickable candidate(s) for keywords=['确定']:\n"
                "[1] <button> role=- locator=probe_idx_3 label=确定"
            ),
            last_action_error="click missed",
        )

    asyncio.run(_run())
    user = seen["messages"][1]["content"]
    assert "DOM PROBE RESULT" in user
    assert "不要重复刚才失败的动作" in user
    assert "click missed" in user
    assert "probe_idx_3" in user
    sys_prompt = seen["messages"][0]["content"]
    assert "DOM PROBE RESULT" in sys_prompt
    assert "only status=\"fail\"" in sys_prompt or "status=\"fail\"" in sys_prompt


def test_nl_goal_fail_triggers_probe_then_decide_receives():
    """click 失败 → 自动 DOM probe → 下一轮 decide 收到 probe_result → 用定位符成功。"""
    from agent.manager import AgentManager
    from app.runtime_config import execution_backend_config
    from core.goal_agent_loop import (
        GoalAction,
        uncovered_checklist_orders,
    )

    mgr = AgentManager()
    steps = [{"step_order": 1, "description": "点击【确定】"}]
    prev = execution_backend_config.dry_run_mode
    execution_backend_config.dry_run_mode = "skip"

    PROBE_TEXT = (
        '{"ok": true, "keywords": ["确定"], "candidates": [{"tag": "button", '
        '"role": "", "name": "", "text": "确定", "placeholder": "", "visible": true, '
        '"candidate_index": 0, "dom_index": 3, "locator": "probe_idx_3"}]}'
    )

    decide_calls = []

    async def _decide(**kwargs):
        decide_calls.append(kwargs)
        journal_tail = kwargs.get("journal_tail") or []
        uncovered = uncovered_checklist_orders(steps, journal_tail)
        if not uncovered:
            return GoalAction(status="done", thinking="all good")
        idx = uncovered[0]
        probe = kwargs.get("probe_result")
        if probe and "DOM PROBE found" in probe:
            return GoalAction(
                status="continue",
                action="click",
                selector="probe_idx_3",
                checklist_index=idx,
                checklist_note=f"checklist item {idx}",
                thinking="use probe locator",
            )
        return GoalAction(
            status="continue",
            action="click",
            selector="e1",
            checklist_index=idx,
            checklist_note=f"checklist item {idx}",
            thinking="cover",
        )

    async def _execute_step_side(session, agent_id, run_id, step_order,
                                 description, tool_call):
        action = tool_call.get("action")
        sel = tool_call.get("selector") or ""
        thinking = str(tool_call.get("thinking") or "")
        if action == "evaluate" and "DOM probe" in thinking:
            return {"success": True, "text": PROBE_TEXT, "action": "evaluate"}
        if action == "click" and sel.startswith("probe_idx"):
            return {"success": True, "text": "ok", "action": f"click({sel})"}
        return {
            "success": False,
            "error": "click missed",
            "text": "no",
            "action": f"click({sel})",
        }

    async def _run():
        session = MagicMock()
        session.agent = MagicMock()
        session.agent.status = MagicMock()
        session.agent.capabilities = ["browser_use", "playwright_mcp"]
        session.request = AsyncMock(return_value={"ready": True})
        session.send = AsyncMock()
        with (
            patch.object(mgr, "get_session", new=AsyncMock(return_value=session)),
            patch.object(mgr, "_get_snapshot", new=AsyncMock(return_value="SNAP")),
            patch.object(
                mgr, "_snapshot_indicates_browser_closed", return_value=False
            ),
            patch(
                "agent.manager.create_openai_client",
                new=AsyncMock(return_value=MagicMock()),
            ),
            patch(
                "agent.manager._llm_resolve_config",
                new=AsyncMock(return_value=(None, None, "fake-model")),
            ),
            patch(
                "core.goal_agent_loop.decide_next_goal_action",
                new=AsyncMock(side_effect=_decide),
            ),
            patch.object(mgr, "_execute_step", new=AsyncMock(side_effect=_execute_step_side)),
            patch(
                "core.script_synthesize.synthesize_playwright_script",
                new=AsyncMock(return_value="async def test_case_42(page):\n    pass\n"),
            ),
            patch.object(
                mgr,
                "_try_run_compiled_script",
                new=AsyncMock(
                    return_value=[
                        {"success": True, "step_number": 1},
                        {"success": True, "step_number": 2},
                    ]
                ),
            ),
        ):
            out = await mgr._execute_on_agent_nl_goal(
                "agent-1", "run-probe", "case", steps,
                case_id=42, navigate_base_url=False,
            )

            # probe 注入到下一轮 decide（第 2 次 decide 调用）
            assert len(decide_calls) >= 2
            probed = next(
                (k for k in decide_calls[1:] if k.get("probe_result")), None
            )
            assert probed is not None
            assert "DOM PROBE found" in probed["probe_result"]
            assert "probe_idx_3" in probed["probe_result"]
            assert probed["last_action_error"] == "click missed"

            # probe 不写 journal：journal 只含 click 失败 / probe 点击成功 / done
            journal_actions = [
                e.get("action")
                for e in (out[0].get("action_journal") or [])
                if e.get("action")
            ]
            assert "evaluate" not in journal_actions
            assert all(r.get("success") for r in out)
            assert all(r.get("backend") == "nl_goal" for r in out)

    try:
        asyncio.run(_run())
    finally:
        execution_backend_config.dry_run_mode = prev


# ---------------------------------------------------------------------------
# Script synthesis quality: journal sanitize + required-target gate
# ---------------------------------------------------------------------------


def _login_steps():
    return [
        {
            "step_order": 1,
            "description": "点击【单位选择】选择京州市院",
            "structured_step": {
                "action": "click",
                "target_name": "单位选择",
                "value": "京州市院",
            },
        },
        {
            "step_order": 2,
            "description": "在【用户名】输入 test1804",
            "structured_step": {"action": "fill", "target_name": "用户名", "value": "test1804"},
        },
        {
            "step_order": 3,
            "description": "在【密码】输入 Abc12345",
            "structured_step": {"action": "fill", "target_name": "密码", "value": "Abc12345"},
        },
        {
            "step_order": 4,
            "description": "点击【登录】",
            "structured_step": {"action": "click", "target_name": "登录"},
        },
        {
            "step_order": 5,
            "description": "点击（页面所有出现的消息的关闭按钮）",
            "structured_step": {"action": "click", "target_name": "关闭"},
        },
    ]


def test_sanitize_journal_keeps_last_cover_and_step_intent():
    from core.script_synthesize import sanitize_journal_for_synth

    steps = _login_steps()
    journal = [
        # step1: first attempt wrong (fill doesn't cover a click, filtered out by covers)
        {"checklist_index": 1, "success": True, "action": "fill", "value": "汉东省院"},
        # step1: correct click
        {"checklist_index": 1, "success": True, "action": "click", "selector": "e1"},
        # step2: intermediate wrong username then real one
        {"checklist_index": 2, "success": True, "action": "fill", "value": "test10"},
        {"checklist_index": 2, "success": True, "action": "fill", "value": "test1804"},
        # noise: no checklist / failed
        {"turn": 9, "success": False, "action": "click", "error": "missed"},
        {"turn": 10, "success": True, "action": "screenshot"},
    ]
    cleaned = sanitize_journal_for_synth(journal, steps)
    idxs = [e["checklist_index"] for e in cleaned]
    assert idxs == [1, 2]
    # step2 keeps only the LAST success (test1804), not test10
    assert cleaned[1]["value"] == "test1804"
    # intent is authoritative from the step, not the journal
    assert "京州市院" in cleaned[0]["intent"]
    assert "test1804" in cleaned[1]["intent"]
    # fill cannot cover click step 1 → dropped
    assert all(e["action"] != "fill" or e["checklist_index"] != 1 for e in cleaned)


def test_extract_required_targets_and_gate():
    from core.script_synthesize import (
        check_script_covers_intents,
        extract_required_targets,
    )

    steps = _login_steps()
    required = extract_required_targets(steps)
    # close-message step (5) excluded; 京州市院/test1804/Abc12345/登录 required
    assert "京州市院" in required
    assert "test1804" in required
    assert "登录" in required
    assert "关闭" not in required

    good = """
import re
from playwright.async_api import expect
async def test_case_5(page):
    await page.locator("input[placeholder='请选择单位']:visible").first.click()
    await page.locator("input[placeholder='输入关键词进行筛选']:not([disabled]):visible").first.press_sequentially("京州市院")
    await page.locator(".el-tree-node__label", has_text=re.compile(r"^京州市院$")).first.click()
    await page.locator("input[placeholder='请输入用户名']:visible").first.fill("test1804")
    await page.locator("input[placeholder='请输入密码']:visible").first.fill("Abc12345")
    await page.locator("button:has-text('登录')").first.click()
"""
    assert check_script_covers_intents(good, steps) == []

    bad = """
async def test_case_5(page):
    await page.locator("span:has-text('汉东省院'):visible").first.click()
    await page.locator("input").first.fill("test10")
"""
    missing = check_script_covers_intents(bad, steps)
    assert "京州市院" in missing
    assert "test1804" in missing


def test_synthesize_payload_has_checklist_and_clean_journal():
    from unittest.mock import AsyncMock, MagicMock

    from core.script_synthesize import synthesize_playwright_script

    # Steps that templates cannot fully cover → LLM path
    weird_steps = [
        {"step_order": 1, "description": "做一件很奇怪的事"},
        {
            "step_order": 2,
            "description": "在【请输入用户名】输入 test1804",
            "structured_step": {
                "action": "fill",
                "target_name": "请输入用户名",
                "value": "test1804",
            },
        },
    ]

    async def _run():
        calls = {}
        good_script = """
async def test_case_5(page):
    await page.locator("input[placeholder='请输入用户名']:visible").first.fill("test1804")
    # 做一件很奇怪的事
    await page.get_by_text("很奇怪的事").first.click()
"""

        async def _create(**kwargs):
            calls["user"] = kwargs["messages"][1]["content"]
            msg = MagicMock()
            msg.content = good_script
            choice = MagicMock()
            choice.message = msg
            resp = MagicMock()
            resp.choices = [choice]
            return resp

        fake = MagicMock()
        fake.chat.completions.create = AsyncMock(side_effect=_create)
        await synthesize_playwright_script(
            client=fake,
            model="x",
            case_id=5,
            case_name="登录",
            goal_text="登录系统",
            journal=[
                {"checklist_index": 2, "success": True, "action": "fill", "value": "test1804"},
                {"turn": 0, "success": False, "action": "click"},
            ],
            steps=weird_steps,
            base_url="http://192.168.9.125/xtmh",
        )
        return calls

    calls = asyncio.run(_run())
    user = calls["user"]
    assert '"checklist"' in user
    assert '"journal_clean"' in user
    assert "test1804" in user
    assert '"success": false' not in user


def test_build_replay_and_codegen_template():
    from core.codegen_locator import merge_codegen_into_replay, load_codegen_iife
    from core.replay_resolve import build_replay_from_step
    from core.script_templates import try_build_templated_script

    assert "resolvePlaywrightLocator" in load_codegen_iife()

    steps = [
        {
            "step_order": 1,
            "description": "点击【请选择单位】",
            "structured_step": {"action": "click", "target_name": "请选择单位"},
        },
        {
            "step_order": 2,
            "description": "在【输入关键词进行筛选】输入 京州市院",
            "structured_step": {
                "action": "fill",
                "target_name": "输入关键词进行筛选",
                "value": "京州市院",
            },
        },
        {
            "step_order": 3,
            "description": "在【京州市院】中选择【京州市院】",
            "structured_step": {
                "action": "select",
                "target_name": "京州市院",
                "value": "京州市院",
            },
        },
        {
            "step_order": 4,
            "description": "在【请输入用户名】输入 test1804",
            "structured_step": {
                "action": "fill",
                "target_name": "请输入用户名",
                "value": "test1804",
            },
        },
        {
            "step_order": 5,
            "description": "在【请输入密码】输入 Abc12345",
            "structured_step": {
                "action": "fill",
                "target_name": "请输入密码",
                "value": "Abc12345",
            },
        },
        {
            "step_order": 6,
            "description": "点击【登录】",
            "structured_step": {"action": "click", "target_name": "登录"},
        },
        {
            "step_order": 7,
            "description": "点击（页面所有出现的消息的关闭按钮）",
            "structured_step": {"action": "click"},
        },
    ]
    r1 = build_replay_from_step(steps[0], action="click", selector="f5e21")
    assert r1["strategy"] == "click_placeholder"
    assert r1.get("css") is None
    r1 = merge_codegen_into_replay(
        r1,
        {
            "ok": True,
            "playwright_locator": 'get_by_placeholder("请选择单位")',
            "locator_candidates": ['get_by_placeholder("请选择单位")'],
        },
    )
    assert r1["playwright_locator"] == 'get_by_placeholder("请选择单位")'

    r3 = build_replay_from_step(steps[2], action="click", selector="f5e193")
    assert r3["strategy"] == "click_text"
    assert r3["exact_text"] == "京州市院"

    def _replay_for(i: int) -> dict:
        st = steps[i].get("structured_step") or {}
        act = str(st.get("action") or "click")
        rp = build_replay_from_step(steps[i], action=act)
        # Simulate codegen locators (no project tree CSS)
        locs = {
            0: 'get_by_placeholder("请选择单位")',
            1: 'get_by_placeholder("输入关键词进行筛选")',
            2: 'get_by_text("京州市院", exact=True)',
            3: 'get_by_placeholder("请输入用户名")',
            4: 'get_by_placeholder("请输入密码")',
            5: 'get_by_role("button", name="登录")',
        }
        if i in locs:
            rp = merge_codegen_into_replay(
                rp, {"ok": True, "playwright_locator": locs[i]}
            )
        return rp

    script = try_build_templated_script(
        case_id=5,
        steps=steps,
        base_url="http://192.168.9.125/xtmh",
        journal=[
            {
                "checklist_index": i,
                "success": True,
                "action": "click",
                "replay": _replay_for(i - 1),
            }
            for i in range(1, 8)
        ],
    )
    assert script is not None
    assert "treeSelect_div" not in script
    assert "get_by_placeholder" in script
    assert "press_sequentially" in script
    assert "京州市院" in script
    assert "test1804" in script
    assert "f5e" not in script
    assert "el-dialog__wrapper" in script
    assert 'page.goto("http://192.168.9.125/"' in script


def test_synthesize_prefers_templates_without_llm():
    from unittest.mock import AsyncMock, MagicMock

    from core.script_synthesize import synthesize_playwright_script

    steps = [
        {
            "step_order": 1,
            "description": "点击【请选择单位】",
            "structured_step": {"action": "click", "target_name": "请选择单位"},
        },
        {
            "step_order": 2,
            "description": "在【输入关键词进行筛选】输入 京州市院",
            "structured_step": {
                "action": "fill",
                "target_name": "输入关键词进行筛选",
                "value": "京州市院",
            },
        },
        {
            "step_order": 3,
            "description": "在【京州市院】中选择【京州市院】",
            "structured_step": {
                "action": "select",
                "target_name": "京州市院",
                "value": "京州市院",
            },
        },
        {
            "step_order": 4,
            "description": "在【请输入用户名】输入 test1804",
            "structured_step": {
                "action": "fill",
                "target_name": "请输入用户名",
                "value": "test1804",
            },
        },
        {
            "step_order": 5,
            "description": "在【请输入密码】输入 Abc12345",
            "structured_step": {
                "action": "fill",
                "target_name": "请输入密码",
                "value": "Abc12345",
            },
        },
        {
            "step_order": 6,
            "description": "点击【登录】",
            "structured_step": {"action": "click", "target_name": "登录"},
        },
        {
            "step_order": 7,
            "description": "点击（页面所有出现的消息的关闭按钮）",
        },
    ]

    async def _run():
        fake = MagicMock()
        fake.chat.completions.create = AsyncMock(
            side_effect=AssertionError("LLM must not be called for templated case")
        )
        return await synthesize_playwright_script(
            client=fake,
            model="x",
            case_id=5,
            case_name="登录",
            goal_text="登录",
            journal=[],
            steps=steps,
            base_url="http://192.168.9.125/xtmh",
        )

    script = asyncio.run(_run())
    assert "treeSelect_div" not in script
    assert "press_sequentially" in script or "get_by_placeholder" in script
    assert "test1804" in script
