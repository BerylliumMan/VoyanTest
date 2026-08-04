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
    assert ExecutionBackendConfig().backend == "nl_goal"


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
    rows = steps_results_from_goal(steps, success=True, journal=[{"turn": 1}])
    assert len(rows) == 2 and all(r["success"] for r in rows)
    assert rows[0].get("action_journal")
    src = _ensure_entrypoint("async def test_case_x(page):\n    await page.goto('/')\n", 9)
    assert "test_case_9" in src


def _ten_steps():
    return [{"step_order": i, "description": f"step{i}"} for i in range(1, 11)]


def test_steps_results_fail_at_9_with_shot():
    """Covered 5–7, unrecovered fail at 9 → 1–8 passed, 9 failed+shot, 10+ skipped."""
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
        ["passed"] * 8 + ["failed"] + ["skipped"]
    )
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


def test_steps_results_success_all_passed():
    journal = [{"checklist_index": i, "success": True} for i in (1, 3, 5)]
    rows = steps_results_from_goal(_ten_steps(), success=True, journal=journal)
    assert all(r["status"] == "passed" and r["success"] for r in rows)
    assert all(r["screenshot_path"] is None for r in rows)


import asyncio
from unittest.mock import AsyncMock, MagicMock, patch


def test_nl_goal_script_verify_fallback_to_hybrid():
    """goal_ok + synthesize/dry-run fail → one legacy hybrid NL re-run."""
    from agent.manager import AgentManager

    mgr = AgentManager()
    steps = [
        {"step_order": 1, "description": "a"},
        {"step_order": 2, "description": "b"},
    ]
    hybrid_rows = [
        {
            "step_number": 1,
            "original_description": "a",
            "success": True,
            "status": "passed",
            "backend": "hybrid",
        },
        {
            "step_number": 2,
            "original_description": "b",
            "success": True,
            "status": "passed",
            "backend": "hybrid",
        },
    ]

    async def _run():
        session = MagicMock()
        session.agent = MagicMock()
        session.agent.status = MagicMock()
        session.request = AsyncMock(return_value={"ready": True})
        session.send = AsyncMock()

        with (
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
                new=AsyncMock(
                    return_value=GoalAction(status="done", thinking="all good")
                ),
            ),
            patch(
                "core.script_synthesize.synthesize_playwright_script",
                new=AsyncMock(side_effect=RuntimeError("synth boom")),
            ),
            patch.object(
                mgr,
                "_execute_on_agent_snapshot_path",
                new=AsyncMock(return_value=hybrid_rows),
            ) as snap,
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
            assert snap.await_args.kwargs.get("selected") == "hybrid" or (
                snap.await_args.args and "hybrid" in str(snap.await_args)
            )
            assert out is hybrid_rows or (
                isinstance(out, list)
                and out[0].get("nl_goal_script_fallback") is True
            )
            assert all(r.get("nl_goal_script_fallback") for r in out)
            assert out[0].get("action_journal")

    asyncio.run(_run())
