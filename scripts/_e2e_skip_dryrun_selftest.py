"""E2E self-test: CLI agent + nl_goal DONE must not relaunch compiled_script.

Proves Cursor semantics (dry_run_mode=skip default):
  after nl_goal DONE → synthesize only → no RUN_COMPILED_SCRIPT /
  \"Running compiled Playwright script\" / \"Trying compiled_script\".

Uses a live Agent WebSocket against localhost:8002. LLM decide/synthesize
are patched on the server-side AgentManager path by running the exercise
in-process against the same code the container runs (imported from /app
when inside Docker, or from repo root on host with mocked agent session).

Mode A (default, --live-agent): start is external; this script drives
manager via a mocked LLM but real agent session from agent_manager.

Mode B (--unit-path): pure in-process mock (no browser) — always runs.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("e2e_skip_dryrun")


def _assert_no_compiled_replay(try_run: AsyncMock, messages: list[str]) -> None:
    try_run.assert_not_awaited()
    joined = "\n".join(messages)
    for bad in (
        "Trying compiled_script",
        "Running compiled Playwright script",
        "compiled_script Using Chromium",
    ):
        assert bad not in joined, f"forbidden log still present: {bad!r}"


async def run_unit_path() -> None:
    """In-process: goal DONE + skip → never _try_run_compiled_script."""
    from agent.manager import AgentManager
    from app.runtime_config import execution_backend_config
    from core.goal_agent_loop import GoalAction, uncovered_checklist_orders

    prev = execution_backend_config.dry_run_mode
    execution_backend_config.dry_run_mode = "skip"
    mgr = AgentManager()
    steps = [
        {"step_order": 1, "description": "open page"},
        {"step_order": 2, "description": "click go"},
    ]
    session = MagicMock()
    session.agent = MagicMock()
    session.agent.status = MagicMock()
    session.agent.capabilities = ["browser_use", "playwright_mcp"]
    session.request = AsyncMock(return_value={"ready": True})
    session.send = AsyncMock()
    try_run = AsyncMock(
        side_effect=AssertionError("must not call _try_run_compiled_script")
    )
    captured: list[str] = []

    async def _decide_cover_then_done(**kwargs):
        uncovered = uncovered_checklist_orders(
            kwargs.get("steps"), kwargs.get("journal_tail") or []
        )
        if uncovered:
            idx = uncovered[0]
            return GoalAction(
                status="continue",
                action="wait",
                value="0",
                checklist_index=idx,
                checklist_note=f"checklist item {idx}",
                thinking=f"cover {idx}",
            )
        return GoalAction(status="done", thinking="all good")

    class _Cap(logging.Handler):
        def emit(self, record):
            captured.append(record.getMessage())

    cap = _Cap()
    logging.getLogger("agent.manager").addHandler(cap)
    try:
        with (
            patch.object(mgr, "get_session", new=AsyncMock(return_value=session)),
            patch.object(mgr, "_get_snapshot", new=AsyncMock(return_value={"ok": True})),
            patch.object(mgr, "_snapshot_indicates_browser_closed", return_value=False),
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
            patch.object(
                mgr,
                "_execute_step",
                new=AsyncMock(return_value={"success": True, "text": "ok"}),
            ),
            patch(
                "core.script_synthesize.synthesize_playwright_script",
                new=AsyncMock(
                    return_value="async def test_case_99(page):\n    pass\n"
                ),
            ),
            patch.object(mgr, "_try_run_compiled_script", new=try_run),
            patch.object(
                mgr, "_execute_on_agent_snapshot_path", new=AsyncMock()
            ),
        ):
            out = await mgr._execute_on_agent_nl_goal(
                "e2e-agent",
                "e2e-run",
                "e2e-case",
                steps,
                case_id=99,
                navigate_base_url=False,
            )
        assert all(r.get("success") for r in out)
        assert mgr._last_synthesized_script
        _assert_no_compiled_replay(try_run, captured)
        assert any("dry_run_mode=skip" in m for m in captured), captured[-10:]
        log.info(
            "PASS unit-path: goal DONE → synthesize (skip) → no compiled_script replay"
        )
        for m in captured:
            if "nl_goal" in m or "dry_run" in m or "compiled" in m:
                log.info("  LOG: %s", m)
    finally:
        logging.getLogger("agent.manager").removeHandler(cap)
        execution_backend_config.dry_run_mode = prev


async def run_live_agent_path(agent_name: str, timeout_s: float = 90.0) -> None:
    """Drive the container's live agent_manager if importable; else fail soft.

    Prefer: docker exec python /app/scripts/... --live-agent
    """
    from agent.manager import agent_manager
    from app.runtime_config import execution_backend_config
    from core.goal_agent_loop import GoalAction, uncovered_checklist_orders

    session = await agent_manager.get_session(agent_name)
    if not session:
        # try any online agent
        sessions = getattr(agent_manager, "_sessions", {}) or {}
        if not sessions:
            raise RuntimeError(
                f"no agent session for {agent_name!r}; start CLI agent first"
            )
        agent_name = next(iter(sessions))
        session = sessions[agent_name]
        log.info("using connected agent %s", agent_name)

    prev = execution_backend_config.dry_run_mode
    execution_backend_config.dry_run_mode = "skip"
    steps = [
        {"step_order": 1, "description": "noop checklist item"},
    ]
    try_run = AsyncMock(
        side_effect=AssertionError("must not call _try_run_compiled_script")
    )
    captured: list[str] = []

    async def _decide_cover_then_done(**kwargs):
        uncovered = uncovered_checklist_orders(
            kwargs.get("steps"), kwargs.get("journal_tail") or []
        )
        if uncovered:
            idx = uncovered[0]
            return GoalAction(
                status="continue",
                action="wait",
                value="0",
                checklist_index=idx,
                checklist_note=f"checklist item {idx}",
                thinking=f"cover {idx}",
            )
        return GoalAction(status="done", thinking="e2e done")

    class _Cap(logging.Handler):
        def emit(self, record):
            captured.append(record.getMessage())

    cap = _Cap()
    logging.getLogger("agent.manager").addHandler(cap)
    run_id = f"e2e{int(time.time()) % 100000}"
    try:
        with (
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
            patch(
                "core.script_synthesize.synthesize_playwright_script",
                new=AsyncMock(
                    return_value="async def test_case_1(page):\n    pass\n"
                ),
            ),
            patch.object(agent_manager, "_try_run_compiled_script", new=try_run),
        ):
            # Real RUN_START on the live CLI agent (may open browser briefly)
            out = await asyncio.wait_for(
                agent_manager._execute_on_agent_nl_goal(
                    agent_name,
                    run_id,
                    "e2e-skip-dryrun",
                    steps,
                    case_id=1,
                    navigate_base_url=False,
                ),
                timeout=timeout_s,
            )
        assert isinstance(out, list) and out and all(r.get("success") for r in out)
        _assert_no_compiled_replay(try_run, captured)
        assert any("dry_run_mode=skip" in m for m in captured), captured[-20:]
        log.info(
            "PASS live-agent: agent=%s run=%s — after DONE no compiled_script replay",
            agent_name,
            run_id,
        )
        for m in captured:
            if any(
                k in m
                for k in (
                    "nl_goal",
                    "dry_run",
                    "compiled",
                    "Trying",
                    "Running compiled",
                )
            ):
                log.info("  LOG: %s", m)
    finally:
        logging.getLogger("agent.manager").removeHandler(cap)
        execution_backend_config.dry_run_mode = prev


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument(
        "--live-agent",
        action="store_true",
        help="Use connected agent_manager session (run inside voyantest container)",
    )
    p.add_argument("--agent-name", default="e2e-skip-dryrun")
    p.add_argument("--unit-path", action="store_true", default=False)
    args = p.parse_args()
    # Always run unit-path proof; optionally also live
    asyncio.run(run_unit_path())
    if args.live_agent:
        asyncio.run(run_live_agent_path(args.agent_name))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
