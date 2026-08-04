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


def test_nl_goal_default_skip_no_compiled_script_replay():
    """Default dry_run_mode=skip: goal DONE → synthesize, never Trying compiled_script."""
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
            side_effect=AssertionError("must not call _try_run_compiled_script")
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
            try_run.assert_not_awaited()
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
