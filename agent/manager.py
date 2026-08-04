"""Agent manager — WebSocket session tracking and step-by-step execution coordination."""

import asyncio
import base64
import json
import logging
import os
import sys
from datetime import datetime, timedelta
from app.tz import now as tz_now
from typing import Dict, List, Optional, Callable, Awaitable
from sqlalchemy import select, text

_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, _project_root)

from agent.models import (
    AgentInfo, AgentStatus, AgentRegistration,
    WSMessage, WSMessageType,
    StepResultPayload, SnapshotPayload, RunCompletePayload,
)

from core.llm_wrapper import create_openai_client, generate_tool_call, _resolve_config as _llm_resolve_config
from core.step_executor import HYBRID_SETTLE_SECONDS
from core.locator_failure import (
    is_locator_failure as _is_locator_failure,
    should_hybrid_browser_use_fallback as _should_hybrid_browser_use_fallback,
)

logger = logging.getLogger("agent.manager")


def _locate_script_verify_failed_step_orders(
    verify: Optional[list],
    steps: List[dict],
    error: str = "",
) -> Optional[List[int]]:
    """Locate 1-based checklist step_orders that script dry-run failed on.

    Returns ``None`` when the failure cannot be attributed to specific steps
    (e.g. whole-script blob with placeholder ``step_number=1`` / infra errors).
    """
    import re

    orders: List[int] = []
    if verify:
        blob = (
            len(verify) == 1
            and isinstance(verify[0], dict)
            and bool(verify[0].get("compiled_script_failed"))
        )
        if not blob:
            for r in verify:
                if not isinstance(r, dict):
                    continue
                sn = r.get("step_number")
                if sn is None:
                    continue
                if not r.get("success") or r.get("compiled_script_failed"):
                    try:
                        orders.append(int(sn))
                    except (TypeError, ValueError):
                        pass

    if not orders and error:
        m = re.search(
            r"(?:checklist|step|步骤)\s*[#:_=-]?\s*(\d+)",
            str(error),
            re.IGNORECASE,
        )
        if m:
            try:
                orders.append(int(m.group(1)))
            except ValueError:
                pass

    if not orders:
        return None

    known: set[int] = set()
    for i, s in enumerate(steps or []):
        so = s.get("step_order") if isinstance(s, dict) else None
        known.add(int(so) if so is not None else i + 1)
    valid = sorted({o for o in orders if o in known})
    return valid or None


def _pick_nl_script_fallback_step(
    steps: List[dict],
    *,
    dry_verify: Optional[list] = None,
    dry_error: str = "",
) -> Optional[tuple]:
    """Choose the single step to hybrid-NL after solidify/dry-run failure.

    Returns ``(step_dict, step_order)`` only when dry-run/error can attribute a
    checklist step. Returns ``None`` when unlocated — caller must keep goal
    results and must not blindly re-run (whole case or last step).
    """
    if not steps:
        return None
    located = _locate_script_verify_failed_step_orders(dry_verify, steps, dry_error)
    if not located:
        return None
    target_order = located[0]
    for i, s in enumerate(steps):
        so = int(s.get("step_order") or s.get("step_number") or i + 1)
        if so == target_order:
            return s, so
    return None


async def _resolve_agent_tool_call(
    *,
    desc: str,
    snap: str,
    expected_result: Optional[str],
    llm_client,
    model: Optional[str],
    base_url_override: Optional[str],
    structured_step: Optional[dict] = None,
    mcp_manager=None,
    prefer_selector: bool = True,
):
    """Intent → unique AX bind; prefer shared resolve_tool_call_from_step (+ vision).

    When ``prefer_selector`` and structured_step has a recorded selector, resolve
    returns that CSS target first (recording solidification).
    """
    from core.blank_click import BLANK_CLICK_ACTION, is_blank_area_click_step
    from core.llm_wrapper import PlaywrightMCPToolCall, generate_tool_call
    from core.step_intent import resolve_tool_call_from_step

    if is_blank_area_click_step(desc):
        return PlaywrightMCPToolCall(
            action=BLANK_CLICK_ACTION,
            selector=None,
            value=None,
            thinking="Step asks to click blank/outside area; use viewport mouse click",
            next_goal="verify this step only",
        )

    try:
        return await resolve_tool_call_from_step(
            desc,
            snap or "",
            expected_result=expected_result,
            mcp_manager=mcp_manager,
            client=llm_client,
            model=model,
            use_vision_fallback=True,
            structured_step=structured_step,
            prefer_selector=prefer_selector,
        )
    except Exception:
        return await generate_tool_call(
            desc, snap, expected_result=expected_result,
            client=llm_client, model=model,
            base_url=base_url_override or None,
        )


class AgentSession:
    """Holds WebSocket send callback and agent metadata for a connected agent."""

    def __init__(self, agent: AgentInfo, send_fn: Callable[[str], Awaitable[None]]):
        self.agent = agent
        self._send = send_fn
        self._pending: Dict[str, asyncio.Future] = {}

    async def send(self, msg: WSMessage):
        try:
            await self._send(msg.model_dump_json())
        except ConnectionError:
            raise
        except Exception as exc:
            # Normalize disconnected transport into ConnectionError for callers
            raise ConnectionError(f"Agent send failed: {exc}") from exc

    async def request(self, msg: WSMessage, timeout: float = 180) -> dict:
        """Send and wait for a reply with matching run_id."""
        key = msg.run_id
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[key] = fut
        try:
            await self.send(msg)
        except Exception:
            self._pending.pop(key, None)
            raise
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(key, None)
            raise
        except asyncio.CancelledError:
            self._pending.pop(key, None)
            raise

    def resolve(self, msg: WSMessage):
        key = msg.run_id
        fut = self._pending.pop(key, None)
        if fut and not fut.done():
            fut.set_result(msg.payload)


class AgentManager:
    """Manages connected agent WebSocket sessions and step-by-step execution."""

    def __init__(self):
        self.sessions: Dict[str, AgentSession] = {}
        self._pending: Dict[str, asyncio.Future] = {}
        self._lock = asyncio.Lock()
        # One client browser run at a time per agent (init must finish before next case).
        self._agent_run_locks: Dict[str, asyncio.Lock] = {}
        self._agent_busy: set[str] = set()
        self._last_compiled_script_failed: Optional[dict] = None
        self._last_action_journal: Optional[list] = None
        self._last_synthesized_script: Optional[dict] = None

    def _run_lock_for(self, agent_id: str) -> asyncio.Lock:
        lock = self._agent_run_locks.get(agent_id)
        if lock is None:
            lock = asyncio.Lock()
            self._agent_run_locks[agent_id] = lock
        return lock

    # ---- session management ----

    async def register(self, agent_id: str, info: AgentRegistration, send_fn) -> AgentInfo:
        agent = AgentInfo(
            id=agent_id or info.name,
            name=info.name,
            hostname=info.hostname,
            ip_address=info.ip_address,
            capabilities=info.capabilities,
            status=AgentStatus.ONLINE,
            last_seen=tz_now(),
        )
        async with self._lock:
            self.sessions[agent.id] = AgentSession(agent, send_fn)
        logger.info(f"Agent registered: {agent.name} ({agent.id})")
        return agent

    async def unregister(self, agent_id: str):
        session = None
        async with self._lock:
            session = self.sessions.pop(agent_id, None)
        # Allow a new run after disconnect; fail any in-flight waits
        self._agent_busy.discard(agent_id)
        if session is not None:
            for _key, fut in list(session._pending.items()):
                if fut and not fut.done():
                    fut.set_exception(ConnectionError(f"Agent {agent_id} disconnected"))
            session._pending.clear()
        logger.info(f"Agent unregistered: {agent_id}")

    async def heartbeat(self, agent_id: str):
        async with self._lock:
            if agent_id in self.sessions:
                self.sessions[agent_id].agent.last_seen = tz_now()

    async def get_online_agents(self) -> List[AgentInfo]:
        now = tz_now()
        result = []
        async with self._lock:
            for s in self.sessions.values():
                if s.agent.last_seen is None:
                    continue
                if (now - s.agent.last_seen).total_seconds() < 120:
                    result.append(s.agent)
        return result

    async def get_session(self, agent_id: str) -> Optional[AgentSession]:
        async with self._lock:
            return self.sessions.get(agent_id)

    # ---- recording (agent-side browser, server-side CDP capture) ----

    async def start_agent_recording(self, agent_id: str, url: str, headless: bool = False) -> str:
        """Ask agent to start Chrome with CDP for recording.

        Returns the CDP WebSocket URL that the server can connect to.
        """
        session = await self.get_session(agent_id)
        if not session:
            raise ValueError(f"Agent {agent_id} not connected")
        session.agent.status = AgentStatus.BUSY
        try:
            run_id = f"rec-{os.urandom(4).hex()}"
            payload = await session.request(WSMessage(
                type=WSMessageType.RECORDING_START, agent_id=agent_id,
                run_id=run_id,
                payload={"url": url, "headless": headless},
            ))
            payload = payload or {}
            status = payload.get("status")
            cdp_url = payload.get("cdp_url")
            if status != "ready" or not cdp_url:
                raise RuntimeError(f"Agent failed to start recording: status={status} has_url={bool(cdp_url)}")
            return cdp_url
        finally:
            session.agent.status = AgentStatus.ONLINE

    async def stop_agent_recording(self, agent_id: str) -> None:
        """Tell agent to stop recording (browser stays alive)."""
        session = await self.get_session(agent_id)
        if not session:
            return
        run_id = f"rec-stop-{os.urandom(4).hex()}"
        try:
            await session.request(WSMessage(
                type=WSMessageType.RECORDING_STOP, agent_id=agent_id,
                run_id=run_id, payload={},
            ))
        except (asyncio.TimeoutError, ValueError):
            pass
        finally:
            session.agent.status = AgentStatus.ONLINE

    # ---- step-by-step execution (server-side LLM, agent-side browser) ----

    async def execute_on_agent(self, agent_id: str, run_id: str,
                                case_name: str, steps: List[dict],
                                output_dir: Optional[str] = None,
                                base_url_override: Optional[str] = None,
                                backend: Optional[str] = None,
                                *,
                                navigate_base_url: bool = True,
                                manage_busy: bool = True,
                                batch_id: Optional[int] = None,
                                case_id: Optional[int] = None,
                                compiled_script: Optional[str] = None,
                                compiled_script_hash: Optional[str] = None,
                                case_description: Optional[str] = None) -> dict:
        """Execute via agent. UI default is nl_goal (Cursor-style); legacy_* for old paths.

        Serialized per agent so batch/init cases never overlap on the same browser.
        Returns a full step results list compatible with the existing report format.
        """
        if manage_busy:
            self._agent_busy.add(agent_id)
        try:
            async with self._run_lock_for(agent_id):
                return await self._execute_on_agent_unlocked(
                    agent_id, run_id, case_name, steps,
                    output_dir=output_dir,
                    base_url_override=base_url_override,
                    backend=backend,
                    navigate_base_url=navigate_base_url,
                    batch_id=batch_id,
                    case_id=case_id,
                    compiled_script=compiled_script,
                    compiled_script_hash=compiled_script_hash,
                    case_description=case_description,
                )
        finally:
            if manage_busy:
                self._agent_busy.discard(agent_id)

    async def _execute_on_agent_unlocked(
        self, agent_id: str, run_id: str,
        case_name: str, steps: List[dict],
        output_dir: Optional[str] = None,
        base_url_override: Optional[str] = None,
        backend: Optional[str] = None,
        navigate_base_url: bool = True,
        batch_id: Optional[int] = None,
        case_id: Optional[int] = None,
        compiled_script: Optional[str] = None,
        compiled_script_hash: Optional[str] = None,
        case_description: Optional[str] = None,
    ) -> dict:
        """Inner implementation; caller must hold ``_run_lock_for(agent_id)``."""
        from app.runtime_config import (
            execution_backend_config,
            normalize_execution_backend,
        )
        from core.compiled_script import steps_content_hash

        selected = normalize_execution_backend(
            backend or execution_backend_config.backend or "nl_goal"
        )
        self._last_action_journal = None
        self._last_synthesized_script = None

        if selected == "browser_use":
            return await self._execute_on_agent_browser_use(
                agent_id, run_id, case_name, steps,
                output_dir=output_dir,
                base_url_override=base_url_override if navigate_base_url else None,
                max_steps_per_nl=execution_backend_config.max_steps_per_nl,
                headless=execution_backend_config.headless,
                navigate_base_url=navigate_base_url,
                batch_id=batch_id,
            )

        # Prefer solidified Playwright script when hash matches current steps
        script = (compiled_script or "").strip()
        script_ok_to_try = bool(script)
        if script_ok_to_try:
            current_hash = steps_content_hash(steps)
            if compiled_script_hash and compiled_script_hash != current_hash:
                logger.info(
                    "compiled_script hash mismatch case=%s stored=%s current=%s — ignore script",
                    case_id, compiled_script_hash[:12], current_hash[:12],
                )
                script = ""
                script_ok_to_try = False

        if script_ok_to_try and script:
            py_results = await self._try_run_compiled_script(
                agent_id, run_id, case_name, steps,
                script=script,
                base_url=base_url_override,
                case_id=case_id,
                steps_hash=compiled_script_hash or steps_content_hash(steps),
            )
            if py_results is not None:
                failed = any(r.get("compiled_script_failed") for r in py_results)
                if not failed and py_results and all(r.get("success") for r in py_results):
                    return py_results
                logger.info("compiled_script failed — fall back (unless compiled_script-only)")
                self._last_compiled_script_failed = {
                    "case_id": case_id,
                    "error": next(
                        (r.get("error") for r in py_results if r.get("error")),
                        "compiled_script failed",
                    ),
                }
                if selected == "compiled_script":
                    return py_results
            else:
                logger.info(
                    "compiled_script unsupported/timeout — fall back"
                )
                if selected == "compiled_script":
                    return [
                        {
                            "step_number": s.get("step_order") or i + 1,
                            "original_description": s.get("description") or "",
                            "success": False,
                            "status": "failed",
                            "thinking": "compiled_script",
                            "action": "compiled_script",
                            "next_goal": "",
                            "error": "compiled_script unsupported or timeout",
                            "screenshot_path": None,
                            "duration_ms": 0,
                            "backend": "compiled_script",
                            "compiled_script_failed": True,
                        }
                        for i, s in enumerate(steps or [])
                    ]

        if selected in ("legacy_hybrid", "legacy_mcp"):
            legacy_backend = "hybrid" if selected == "legacy_hybrid" else "playwright_mcp"
            return await self._execute_on_agent_snapshot_path(
                agent_id, run_id, case_name, steps,
                output_dir=output_dir,
                base_url_override=base_url_override,
                backend=legacy_backend,
                navigate_base_url=navigate_base_url,
                batch_id=batch_id,
                selected=legacy_backend,
            )

        # Default / nl_goal: Cursor-style whole-case goal loop
        return await self._execute_on_agent_nl_goal(
            agent_id, run_id, case_name, steps,
            output_dir=output_dir,
            base_url_override=base_url_override,
            navigate_base_url=navigate_base_url,
            batch_id=batch_id,
            case_id=case_id,
            case_description=case_description,
        )

    async def _execute_on_agent_nl_goal(
        self,
        agent_id: str,
        run_id: str,
        case_name: str,
        steps: List[dict],
        output_dir: Optional[str] = None,
        base_url_override: Optional[str] = None,
        navigate_base_url: bool = True,
        batch_id: Optional[int] = None,
        case_id: Optional[int] = None,
        case_description: Optional[str] = None,
    ) -> list:
        """Cursor-style whole-case NL goal loop → journal → synthesize script.

        After goal DONE, report is always based on the journal (passed). Script
        solidify follows ``execution_backend_config.dry_run_mode``:

        - ``skip`` (default): synthesize only; never launch a second Chromium /
          whole-case ``compiled_script`` replay (what looked like "restarting
          from step 1"). Persist synthesized script without isolated verify.
        - ``attach``: reserved; currently same as skip (no whole-case relaunch).
        - ``isolated``: optional headless dry-run(+one repair). On verify fail,
          may remediate one located checklist step via hybrid on the *same*
          browser; never re-RUN_START the whole case. Fail → warning, no persist.
        """
        import time as _time

        from app.runtime_config import execution_backend_config
        from core.compiled_script import steps_content_hash
        from core.goal_agent_loop import (
            CLOSE_ALL_PAGE_PROMPTS_JS,
            DEFAULT_MAX_TURNS,
            GoalAction,
            build_goal_text,
            close_messages_step_orders,
            decide_next_goal_action,
            detect_stagnation,
            journal_entry,
            seed_open_steps_after_navigation,
            steps_results_from_goal,
            tool_call_from_decision,
            uncovered_checklist_orders,
        )
        from core.script_synthesize import repair_playwright_script, synthesize_playwright_script

        session = await self.get_session(agent_id)
        if not session:
            raise ValueError(f"Agent {agent_id} not connected")

        # Prefer hybrid shared-CDP so script-verify step fallback can reuse the
        # same Chromium/page. Plain playwright_mcp has no CDP endpoint; a later
        # hybrid RUN_START would launch a blank browser.
        caps = session.agent.capabilities or []
        nl_backend = (
            "hybrid" if "browser_use" in caps else "playwright_mcp"
        )

        max_turns = int(
            getattr(execution_backend_config, "max_steps_per_nl", None) or DEFAULT_MAX_TURNS
        )
        # Whole-case NL needs more turns than per-step hybrid rescue
        max_turns = max(DEFAULT_MAX_TURNS, max_turns)
        goal_text = build_goal_text(
            case_name=case_name,
            description=case_description,
            steps=steps,
        )
        journal: list = []
        goal_error: Optional[str] = None
        goal_ok = False
        run_started = False

        session.agent.status = AgentStatus.BUSY
        logger.info(
            "nl_goal start case=%s agent=%s max_turns=%s backend=%s",
            case_id or case_name,
            agent_id,
            max_turns,
            nl_backend,
        )

        try:
            ready = await session.request(
                WSMessage(
                    type=WSMessageType.RUN_START,
                    agent_id=agent_id,
                    run_id=run_id,
                    payload={
                        "case_id": run_id,
                        "case_name": case_name,
                        "steps": steps,
                        "base_url": (base_url_override or "") if navigate_base_url else "",
                        "backend": nl_backend,
                        "navigate_base_url": navigate_base_url,
                    },
                ),
                timeout=90,
            )
            run_started = True
            if isinstance(ready, dict) and ready.get("ready") is False:
                err = ready.get("error") or ready.get("message") or "browser not ready"
                raise RuntimeError(f"Agent browser failed to start: {err}")
            if isinstance(ready, dict) and ready.get("message") and "MCP start failed" in str(
                ready.get("message")
            ):
                raise RuntimeError(str(ready.get("message")))

            llm_client = await create_openai_client()
            _, _, model = await _llm_resolve_config()

            await self._get_snapshot(session, agent_id, run_id)
            if navigate_base_url and base_url_override:
                nav_result = await self.send_act(
                    agent_id,
                    run_id,
                    {
                        "name": "browser_navigate",
                        "tool": "browser_navigate",
                        "args": {"value": base_url_override, "url": base_url_override},
                    },
                )
                if not nav_result.get("success"):
                    reason = nav_result.get("error") or "unknown"
                    raise RuntimeError(
                        f"导航失败，已停止执行: {base_url_override} — {reason}"
                    )
                journal.extend(
                    seed_open_steps_after_navigation(steps, base_url_override)
                )

            close_helper_runs = 0
            for turn in range(1, max_turns + 1):
                if batch_id is not None:
                    from app import execution_control

                    await execution_control.wait_if_paused(batch_id)
                    if execution_control.is_stopped(batch_id):
                        goal_error = "用户停止执行"
                        break

                snap = await self._get_snapshot(session, agent_id, run_id)
                if self._snapshot_indicates_browser_closed(snap):
                    goal_error = "浏览器已关闭"
                    break

                # Cursor pattern: AFTER earlier checklist is done, when
                # close-messages is the next uncovered item, run proven
                # Element UI dismiss JS (dialogs + notifications) — do not
                # let the LLM click the message bell / 去查看.
                uncovered_now = uncovered_checklist_orders(steps, journal)
                close_orders = [
                    o
                    for o in close_messages_step_orders(steps)
                    if o in set(uncovered_now)
                ]
                close_idx = min(close_orders) if close_orders else None
                earlier_blocking = (
                    [o for o in uncovered_now if o < close_idx]
                    if close_idx is not None
                    else uncovered_now
                )
                use_close_helper = (
                    close_idx is not None
                    and not earlier_blocking
                    and close_helper_runs < 3
                )
                if use_close_helper:
                    close_helper_runs += 1
                    logger.info(
                        "nl_goal Cursor-style close_all_prompts helper "
                        "case=%s turn=%s step=%s run=%s",
                        case_id,
                        turn,
                        close_idx,
                        close_helper_runs,
                    )
                    decision = GoalAction(
                        status="continue",
                        thinking=(
                            "Cursor-style: dismiss all visible Element UI "
                            "dialogs/notifications via evaluate click loop"
                        ),
                        action="evaluate",
                        selector="",
                        value=CLOSE_ALL_PAGE_PROMPTS_JS,
                        stable_hint="CLOSE_ALL_PAGE_PROMPTS",
                        checklist_index=close_idx,
                        checklist_note=(
                            f"CLOSE_ALL_PAGE_PROMPTS checklist item {close_idx}"
                        ),
                    )
                else:
                    try:
                        decision = await decide_next_goal_action(
                            client=llm_client,
                            model=model,
                            goal_text=goal_text,
                            snapshot=snap,
                            journal_tail=journal,
                            steps=steps,
                        )
                    except Exception as exc:
                        goal_error = f"goal LLM failed: {exc}"
                        logger.exception("nl_goal decide failed turn=%s", turn)
                        break

                if decision.status == "done":
                    uncovered = uncovered_checklist_orders(steps, journal)
                    if uncovered:
                        logger.warning(
                            "nl_goal rejecting premature DONE case=%s turn=%s "
                            "uncovered=%s",
                            case_id,
                            turn,
                            uncovered,
                        )
                        journal.append(
                            {
                                "turn": turn,
                                "status": "done_rejected",
                                "thinking": decision.thinking,
                                "action": None,
                                "selector": None,
                                "value": None,
                                "stable_hint": None,
                                "checklist_index": None,
                                "checklist_note": (
                                    f"premature DONE rejected; still uncovered: "
                                    f"{uncovered}"
                                ),
                                "success": False,
                                "error": (
                                    f"premature DONE: checklist steps "
                                    f"{uncovered} not yet executed"
                                ),
                                "duration_ms": 0,
                                "result_snippet": None,
                                "screenshot_on_fail": False,
                                "screenshot_path": None,
                            }
                        )
                        continue

                    journal.append(
                        journal_entry(
                            turn=turn,
                            decision=decision,
                            success=True,
                        )
                    )
                    goal_ok = True
                    logger.info("nl_goal DONE case=%s turns=%s", case_id, turn)
                    break

                async def _save_nl_goal_fail_shot(
                    entry: dict,
                    *,
                    turn_n: int,
                    checklist_idx,
                    fallback_b64: Optional[str] = None,
                ) -> None:
                    """Capture fail screenshot into output_dir/screenshots (legacy-style path)."""
                    try:
                        shot = await self._get_screenshot(session, agent_id, run_id)
                        b64 = None
                        if isinstance(shot, dict):
                            b64 = shot.get("screenshot_base64") or shot.get("base64")
                        if not b64:
                            b64 = fallback_b64
                        if b64 and output_dir:
                            ss_dir = os.path.join(output_dir, "screenshots")
                            os.makedirs(ss_dir, exist_ok=True)
                            idx = checklist_idx or turn_n
                            ss_path = os.path.join(
                                ss_dir, f"nl_goal_step_{idx}_turn_{turn_n}.png"
                            )
                            with open(ss_path, "wb") as f:
                                f.write(base64.b64decode(b64))
                            # FE loads /{screenshot_path}; output_dir is reports/run_...
                            entry["screenshot_path"] = ss_path.replace("\\", "/")
                            entry["screenshot_on_fail"] = True
                        elif b64:
                            entry["screenshot_on_fail"] = True
                    except Exception:
                        logger.debug("nl_goal fail screenshot skipped", exc_info=True)

                if decision.status == "fail":
                    goal_error = decision.reason or decision.thinking or "nl_goal fail"
                    entry = journal_entry(
                        turn=turn,
                        decision=decision,
                        success=False,
                        error=goal_error,
                    )
                    await _save_nl_goal_fail_shot(
                        entry,
                        turn_n=turn,
                        checklist_idx=decision.checklist_index,
                    )
                    journal.append(entry)
                    break

                action = (decision.action or "").strip().lower()
                if not action or action == "done":
                    uncovered = uncovered_checklist_orders(steps, journal)
                    if uncovered:
                        logger.warning(
                            "nl_goal rejecting empty/done action case=%s turn=%s "
                            "uncovered=%s",
                            case_id,
                            turn,
                            uncovered,
                        )
                        journal.append(
                            {
                                "turn": turn,
                                "status": "done_rejected",
                                "thinking": decision.thinking,
                                "action": action or None,
                                "selector": None,
                                "value": None,
                                "stable_hint": None,
                                "checklist_index": None,
                                "checklist_note": (
                                    f"premature done action rejected; still "
                                    f"uncovered: {uncovered}"
                                ),
                                "success": False,
                                "error": (
                                    f"premature DONE: checklist steps "
                                    f"{uncovered} not yet executed"
                                ),
                                "duration_ms": 0,
                                "result_snippet": None,
                                "screenshot_on_fail": False,
                                "screenshot_path": None,
                            }
                        )
                        continue
                    journal.append(
                        journal_entry(turn=turn, decision=decision, success=True)
                    )
                    goal_ok = True
                    break

                tc = tool_call_from_decision(decision)
                t0 = _time.monotonic()
                result = await self._execute_step(
                    session,
                    agent_id,
                    run_id,
                    turn,
                    decision.checklist_note or decision.thinking or action,
                    tc,
                )
                dur = (_time.monotonic() - t0) * 1000
                ok = bool(result.get("success"))
                err = result.get("error")
                if self._error_indicates_browser_closed(err):
                    goal_error = err or "浏览器已关闭"
                    journal.append(
                        journal_entry(
                            turn=turn,
                            decision=decision,
                            success=False,
                            error=goal_error,
                            duration_ms=dur,
                        )
                    )
                    break

                entry = journal_entry(
                    turn=turn,
                    decision=decision,
                    success=ok,
                    error=None if ok else (err or "action failed"),
                    duration_ms=dur,
                    result_snippet=str(
                        result.get("text")
                        or result.get("thinking")
                        or result.get("action")
                        or ""
                    ),
                    screenshot_on_fail=False,
                    screenshot_path=None,
                )
                if not ok:
                    await _save_nl_goal_fail_shot(
                        entry,
                        turn_n=turn,
                        checklist_idx=decision.checklist_index,
                        fallback_b64=result.get("screenshot_base64"),
                    )
                journal.append(entry)
                logger.info(
                    "nl_goal turn=%s %s %s selector=%r ok=%s",
                    turn,
                    action,
                    "✓" if ok else "✗",
                    (decision.selector or "")[:80],
                    ok,
                )

                if detect_stagnation(journal):
                    goal_error = "nl_goal stagnation — repeated failing/identical actions"
                    logger.warning(goal_error)
                    break

            else:
                goal_error = goal_error or f"nl_goal hit max_turns={max_turns}"

            # Ensure a failure screenshot exists when goal did not complete
            if not goal_ok and output_dir and journal:
                has_shot = any(e.get("screenshot_path") for e in journal)
                if not has_shot:
                    try:
                        shot = await self._get_screenshot(session, agent_id, run_id)
                        b64 = (shot or {}).get("screenshot_base64") if isinstance(shot, dict) else None
                        if b64:
                            ss_dir = os.path.join(output_dir, "screenshots")
                            os.makedirs(ss_dir, exist_ok=True)
                            ss_path = os.path.join(ss_dir, "nl_goal_final_fail.png")
                            with open(ss_path, "wb") as f:
                                f.write(base64.b64decode(b64))
                            journal[-1]["screenshot_path"] = ss_path.replace("\\", "/")
                            journal[-1]["screenshot_on_fail"] = True
                    except Exception:
                        logger.debug("nl_goal final fail screenshot skipped", exc_info=True)

            # Do NOT RUN_END here — synthesize dry-run is separate, but the
            # single-step hybrid fallback must reuse this same browser/page.
            self._last_action_journal = journal

            if not goal_ok:
                return steps_results_from_goal(
                    steps,
                    success=False,
                    journal=journal,
                    error=goal_error or "nl_goal failed",
                    backend="nl_goal",
                )

            # Success → synthesize Playwright; verify only when dry_run_mode=isolated
            results = steps_results_from_goal(
                steps, success=True, journal=journal, backend="nl_goal"
            )
            if not all(
                isinstance(r, dict) and r.get("success") for r in (results or [])
            ):
                logger.warning(
                    "nl_goal DONE but some checklist steps not truly covered — "
                    "report keeps failures; skip script solidify case=%s",
                    case_id,
                )
                return results
            script_ok = False
            dry_verify_last: Optional[list] = None
            dry_error_last = ""
            dry_mode = (
                getattr(execution_backend_config, "dry_run_mode", None) or "skip"
            )
            dry_mode = str(dry_mode).strip().lower()
            if dry_mode not in ("skip", "attach", "isolated"):
                dry_mode = "skip"
            # attach: same browser verify not wired yet — never fall back to
            # isolated whole-case Chromium relaunch (user complaint path).
            if dry_mode == "attach":
                logger.info(
                    "nl_goal dry_run_mode=attach not implemented — "
                    "treating as skip (no whole-case compiled_script replay)"
                )
                dry_mode = "skip"

            try:
                llm_client = await create_openai_client()
                _, _, model = await _llm_resolve_config()
                cid = int(case_id or 0)
                script = await synthesize_playwright_script(
                    client=llm_client,
                    model=model,
                    case_id=cid or 1,
                    case_name=case_name,
                    goal_text=goal_text,
                    journal=journal,
                    base_url=base_url_override,
                )

                if dry_mode == "skip":
                    # Cursor semantics: goal DONE is the verdict. Persist script
                    # from journal without a second browser replay from step 1.
                    if script:
                        h = steps_content_hash(steps)
                        self._last_synthesized_script = {
                            "case_id": case_id,
                            "script": script,
                            "steps_hash": h,
                        }
                        script_ok = True
                        logger.info(
                            "nl_goal synthesized script ok case=%s bytes=%s "
                            "(dry_run_mode=skip — no compiled_script replay)",
                            case_id,
                            len(script),
                        )
                    return results

                # dry_run_mode=isolated only: optional headless whole-script verify
                verify = await self._try_run_compiled_script(
                    agent_id,
                    f"{run_id}_dry",
                    case_name,
                    steps,
                    script=script,
                    base_url=base_url_override,
                    case_id=cid or None,
                    steps_hash=steps_content_hash(steps),
                )
                dry_verify_last = verify
                dry_fail = (
                    verify is None
                    or any(r.get("compiled_script_failed") for r in (verify or []))
                    or not (verify and all(r.get("success") for r in verify))
                )
                if dry_fail:
                    err = ""
                    if verify:
                        err = next(
                            (r.get("error") for r in verify if r.get("error")),
                            "dry-run failed",
                        )
                    else:
                        err = "dry-run unsupported/timeout"
                    # Truncate — Playwright missing-browser dumps a huge box
                    err_short = " ".join(str(err).split())[:180]
                    dry_error_last = err_short
                    logger.warning(
                        "nl_goal script dry-run failed — repair once: %s", err_short
                    )
                    try:
                        script = await repair_playwright_script(
                            client=llm_client,
                            model=model,
                            case_id=cid or 1,
                            script=script,
                            error=err_short,
                            journal=journal,
                        )
                        verify2 = await self._try_run_compiled_script(
                            agent_id,
                            f"{run_id}_dry2",
                            case_name,
                            steps,
                            script=script,
                            base_url=base_url_override,
                            case_id=cid or None,
                            steps_hash=steps_content_hash(steps),
                        )
                        dry_verify_last = verify2
                        dry_fail = (
                            verify2 is None
                            or any(r.get("compiled_script_failed") for r in (verify2 or []))
                            or not (verify2 and all(r.get("success") for r in verify2))
                        )
                        if dry_fail and verify2:
                            err2 = next(
                                (r.get("error") for r in verify2 if r.get("error")),
                                "",
                            )
                            if err2:
                                dry_error_last = " ".join(str(err2).split())[:180]
                    except Exception:
                        logger.warning("script repair failed", exc_info=True)
                        dry_fail = True

                if not dry_fail and script:
                    h = steps_content_hash(steps)
                    self._last_synthesized_script = {
                        "case_id": case_id,
                        "script": script,
                        "steps_hash": h,
                    }
                    script_ok = True
                    logger.info(
                        "nl_goal synthesized script ok case=%s bytes=%s",
                        case_id,
                        len(script),
                    )
                else:
                    logger.warning(
                        "nl_goal goal passed but script verify failed — not persisting"
                    )
            except Exception:
                logger.warning("nl_goal synthesize/verify failed", exc_info=True)
                script_ok = False

            # isolated verify failed → hybrid NL for the located failed step only
            # (do NOT re-RUN_START the whole case). Unlocated → keep goal results.
            if not script_ok and dry_mode == "isolated":
                picked = _pick_nl_script_fallback_step(
                    steps,
                    dry_verify=dry_verify_last,
                    dry_error=dry_error_last,
                )
                if not picked:
                    logger.warning(
                        "nl_goal script verify failed — cannot locate failed step; "
                        "keeping goal success results (no whole-case / blind hybrid re-run)"
                    )
                    for r in results:
                        if isinstance(r, dict):
                            r["nl_goal_script_fallback_attempted"] = True
                            r["nl_goal_script_verify_unlocated"] = True
                    return results
                fb_step, fb_order = picked
                logger.warning(
                    "nl_goal script verify failed — remediating located step %s only",
                    fb_order,
                )
                try:
                    nl_results = await self._execute_on_agent_snapshot_path(
                        agent_id,
                        f"{run_id}_nl_fb",
                        case_name,
                        [fb_step],
                        output_dir=output_dir,
                        base_url_override=base_url_override,
                        backend="hybrid",
                        navigate_base_url=False,
                        reuse_existing_browser=True,
                        batch_id=batch_id,
                        selected="hybrid",
                    )
                    if isinstance(nl_results, list) and nl_results:
                        fb_row = next(
                            (
                                r
                                for r in nl_results
                                if isinstance(r, dict)
                                and int(r.get("step_number") or 0) == int(fb_order)
                            ),
                            nl_results[0] if isinstance(nl_results[0], dict) else None,
                        )
                        if not isinstance(fb_row, dict):
                            logger.warning(
                                "nl_goal NL fallback returned no usable step row — "
                                "keeping goal results"
                            )
                        else:
                            fb_row.setdefault("backend", "legacy_hybrid")
                            fb_row["nl_goal_script_fallback"] = True
                            fb_row["step_number"] = int(fb_order)
                            fb_ok_step = bool(fb_row.get("success"))
                            if not fb_ok_step:
                                logger.warning(
                                    "nl_goal NL fallback step %s did not pass — "
                                    "keeping goal success results (script not persisted)",
                                    fb_order,
                                )
                                for r in results:
                                    if isinstance(r, dict):
                                        r["nl_goal_script_fallback_attempted"] = True
                                return results

                            # Merge: keep goal-passed rows for other steps
                            merged: list = []
                            replaced = False
                            for r in results:
                                if not isinstance(r, dict):
                                    continue
                                if int(r.get("step_number") or 0) == int(fb_order):
                                    merged.append(fb_row)
                                    replaced = True
                                else:
                                    merged.append(r)
                            if not replaced:
                                merged.append(fb_row)
                            if journal and merged:
                                merged[0]["action_journal"] = journal

                            # Secondary solidify from merged locators — still no
                            # whole-case dry-run under skip; only when isolated.
                            try:
                                from core.compiled_script import build_script_from_run

                                cid = int(case_id or 0)
                                h = steps_content_hash(steps)
                                built = build_script_from_run(
                                    case_id=cid or 1,
                                    case_name=case_name,
                                    steps=steps,
                                    step_results=merged,
                                    base_url=base_url_override,
                                    steps_hash=h,
                                )
                                if built:
                                    # Persist without another isolated Chromium
                                    # relaunch — goal already passed; locators
                                    # came from the live page remediation.
                                    self._last_synthesized_script = {
                                        "case_id": case_id,
                                        "script": built,
                                        "steps_hash": h,
                                    }
                                    logger.info(
                                        "nl_goal fallback solidify ok case=%s bytes=%s "
                                        "(no secondary compiled_script replay)",
                                        case_id,
                                        len(built),
                                    )
                            except Exception:
                                logger.warning(
                                    "nl_goal fallback solidify skipped",
                                    exc_info=True,
                                )
                            return merged
                    logger.warning(
                        "nl_goal NL fallback returned empty — keeping goal results"
                    )
                except Exception:
                    logger.warning(
                        "nl_goal script-verify NL fallback failed — keeping goal results",
                        exc_info=True,
                    )

            return results

        except ConnectionError:
            raise
        except Exception as exc:
            goal_error = str(exc)
            logger.exception("nl_goal aborted: %s", exc)
            self._last_action_journal = journal
            return steps_results_from_goal(
                steps,
                success=False,
                journal=journal,
                error=goal_error or "nl_goal failed",
                backend="nl_goal",
            )
        finally:
            if run_started:
                try:
                    await session.send(
                        WSMessage(
                            type=WSMessageType.RUN_END,
                            agent_id=agent_id,
                            run_id=run_id,
                        )
                    )
                except Exception:
                    logger.debug("RUN_END after nl_goal failed", exc_info=True)
            session.agent.status = AgentStatus.ONLINE

    async def _try_run_compiled_script(
        self,
        agent_id: str,
        run_id: str,
        case_name: str,
        steps: List[dict],
        *,
        script: str,
        base_url: Optional[str],
        case_id: Optional[int],
        steps_hash: str,
    ) -> Optional[list]:
        """Ask agent to run solidified Playwright script. None = fall back."""
        session = await self.get_session(agent_id)
        if not session:
            return None
        try:
            session.agent.status = AgentStatus.BUSY
            logger.info(
                "Trying compiled_script case=%s agent=%s hash=%s",
                case_id, agent_id, (steps_hash or "")[:12],
            )
            resp = await session.request(WSMessage(
                type=WSMessageType.RUN_COMPILED_SCRIPT,
                agent_id=agent_id,
                run_id=run_id,
                payload={
                    "script": script,
                    "base_url": base_url or "",
                    "case_id": case_id or 0,
                    "steps_hash": steps_hash,
                    "case_name": case_name,
                },
            ), timeout=600)
        except Exception as exc:
            logger.warning("compiled_script request failed: %s", exc)
            return None
        finally:
            session.agent.status = AgentStatus.ONLINE

        if not isinstance(resp, dict):
            return None
        if resp.get("unsupported"):
            return None
        if not resp.get("success"):
            logger.warning(
                "compiled_script run failed: %s",
                " ".join(str(resp.get("error") or "").split())[:180],
            )
            return [
                {
                    "step_number": 1,
                    "original_description": case_name,
                    "success": False,
                    "thinking": "compiled_script",
                    "action": "compiled_script",
                    "next_goal": "",
                    "error": resp.get("error") or "compiled_script failed",
                    "screenshot_path": None,
                    "duration_ms": float(resp.get("duration_ms") or 0),
                    "backend": "compiled_script",
                    "compiled_script_failed": True,
                }
            ]

        results = []
        for s in steps:
            results.append({
                "step_number": s.get("step_order"),
                "step_id": s.get("id"),
                "original_description": s.get("description"),
                "success": True,
                "thinking": "compiled_script",
                "action": "compiled_script",
                "next_goal": "",
                "error": None,
                "screenshot_path": None,
                "duration_ms": 0,
                "backend": "compiled_script",
                "resolved_selector": (
                    (s.get("structured_step") or {}).get("selector")
                    if isinstance(s.get("structured_step"), dict) else None
                ),
            })
        logger.info("compiled_script SUCCESS case=%s steps=%s", case_id, len(results))
        return results

    async def _execute_on_agent_snapshot_path(
        self, agent_id: str, run_id: str,
        case_name: str, steps: List[dict],
        output_dir: Optional[str] = None,
        base_url_override: Optional[str] = None,
        backend: Optional[str] = None,
        navigate_base_url: bool = True,
        batch_id: Optional[int] = None,
        selected: Optional[str] = None,
        reuse_existing_browser: bool = False,
    ) -> dict:
        """LEGACY Snapshot → bind ref → MCP (hybrid browser-use fallback).

        Prefer ``_execute_on_agent_nl_goal`` for UI. Kept for ``legacy_hybrid`` /
        ``legacy_mcp`` backends only.

        ``reuse_existing_browser``: for nl_goal step fallback — tell the agent to
        keep the current Chromium/page (do not launch a fresh Hybrid CDP browser).
        """
        from app.runtime_config import execution_backend_config

        selected = (selected or backend or execution_backend_config.backend or "playwright_mcp").strip()
        session = await self.get_session(agent_id)
        if not session:
            raise ValueError(f"Agent {agent_id} not connected")

        hybrid = selected == "hybrid"
        if hybrid and "browser_use" not in (session.agent.capabilities or []):
            logger.warning(
                "hybrid requested but agent %s lacks browser_use capability; MCP only",
                agent_id,
            )
            hybrid = False

        logger.info(
            "Client agent run backend selected=%s hybrid=%s agent=%s caps=%s reuse=%s",
            selected,
            hybrid,
            agent_id,
            session.agent.capabilities or [],
            reuse_existing_browser,
        )

        session.agent.status = AgentStatus.BUSY
        step_results = []
        consecutive_failures = 0
        max_failures = 1
        failed_step_number = None
        # nl_goal step fallback: never RUN_START/RUN_END — parent keeps the session.
        # Sending hybrid RUN_START would launch a blank Chromium on agents that
        # started nl_goal with plain MCP (no shared CDP).
        own_run_lifecycle = not reuse_existing_browser

        try:
            if reuse_existing_browser:
                logger.info(
                    "reuse existing browser for nl_goal step fallback run=%s "
                    "(skip RUN_START / no new Chromium / no BASE URL nav)",
                    run_id,
                )
            else:
                # Wait until client browser/MCP is ready (ACK via SNAPSHOT_RESULT)
                ready = await session.request(
                    WSMessage(
                        type=WSMessageType.RUN_START, agent_id=agent_id,
                        run_id=run_id,
                        payload={
                            "case_id": run_id,
                            "case_name": case_name,
                            "steps": steps,
                            "base_url": (base_url_override or "") if navigate_base_url else "",
                            "backend": "hybrid" if hybrid else "playwright_mcp",
                            "navigate_base_url": navigate_base_url,
                            "reuse_existing_browser": False,
                        },
                    ),
                    timeout=90,
                )
                if isinstance(ready, dict) and ready.get("ready") is False:
                    err = ready.get("error") or ready.get("message") or ready.get("text") or "browser not ready"
                    logger.error("Agent %s browser failed to start: %s", agent_id, err)
                    err_s = str(err)
                    if err_s.startswith("导航失败"):
                        raise RuntimeError(err_s)
                    raise RuntimeError(f"Agent browser failed to start: {err_s}")
                if isinstance(ready, dict) and ready.get("message") and "MCP start failed" in str(ready.get("message")):
                    raise RuntimeError(str(ready.get("message")))

            llm_client = await create_openai_client()
            _, _, model = await _llm_resolve_config()

            # 等待浏览器就绪；仅首个用例（或显式要求时）导航 BASE URL
            await self._get_snapshot(session, agent_id, run_id)
            if reuse_existing_browser:
                pass  # stay on nl_goal's current page
            elif navigate_base_url and base_url_override:
                logger.info("Playwright navigating to BASE URL: %s", base_url_override)
                nav_result = await self.send_act(
                    agent_id,
                    run_id,
                    {
                        "name": "browser_navigate",
                        "tool": "browser_navigate",
                        "args": {"value": base_url_override, "url": base_url_override},
                    },
                )
                if not nav_result.get("success"):
                    reason = nav_result.get("error") or "unknown"
                    msg = f"导航失败，已停止执行: {base_url_override} — {reason}"
                    logger.error("BASE URL Playwright navigation failed — aborting run: %s", reason)
                    raise RuntimeError(msg)
            elif not navigate_base_url:
                logger.info(
                    "Batch follow-up case %r — skip BASE URL navigation (keep session)",
                    case_name,
                )
            else:
                logger.warning(
                    "No BASE URL for run %s — browser may stay on about:blank",
                    run_id,
                )

            for idx, step in enumerate(steps):
                if batch_id is not None:
                    from app import execution_control
                    await execution_control.wait_if_paused(batch_id)
                    if execution_control.is_stopped(batch_id):
                        try:
                            await session.send(WSMessage(
                                type=WSMessageType.CANCEL_RUN,
                                agent_id=agent_id,
                                run_id=run_id,
                                payload={},
                            ))
                        except Exception:
                            logger.debug("CANCEL_RUN send failed", exc_info=True)
                        for rest in steps[idx:]:
                            step_results.append({
                                "step_number": rest["step_order"],
                                "original_description": rest["description"],
                                "success": False,
                                "status": "cancelled",
                                "thinking": "",
                                "action": "",
                                "next_goal": "",
                                "error": "用户停止执行",
                                "screenshot_path": None,
                                "duration_ms": 0,
                            })
                        break

                if failed_step_number is not None:
                    step_results.append({
                        "step_number": step["step_order"],
                        "original_description": step["description"],
                        "success": False,
                        "status": "skipped",
                        "thinking": "",
                        "action": "",
                        "next_goal": "",
                        "error": f"Skipped due to step {failed_step_number} failure",
                        "screenshot_path": None,
                        "duration_ms": 0,
                    })
                    continue

                step_order = step["step_order"]
                desc = step["description"]
                expected_result = step.get("expected_result")
                cached_fp = step.get("learned_locator") if isinstance(step.get("learned_locator"), dict) else None
                cacheable = bool(step.get("cacheable", True))
                structured_step = step.get("structured_step") if isinstance(step.get("structured_step"), dict) else None
                if structured_step is None and desc:
                    try:
                        from core.step_normalize import parse_instant_to_structured, sanitize_ui_step
                        structured_step = parse_instant_to_structured(sanitize_ui_step(desc))
                    except Exception:
                        structured_step = None

                logger.info(f"--- Step {step_order}: {desc} ---")

                # 1. Get DOM snapshot from agent's browser
                snap = await self._get_snapshot(session, agent_id, run_id)
                if self._snapshot_indicates_browser_closed(snap):
                    logger.warning(
                        "Step %s aborted — browser closed by user", step_order,
                    )
                    step_results.append({
                        "step_number": step_order,
                        "original_description": desc,
                        "success": False,
                        "thinking": "",
                        "action": "",
                        "next_goal": "",
                        "error": "浏览器已关闭，用例执行已中断",
                        "screenshot_path": None,
                        "duration_ms": 0,
                        "failure_kind": "browser_closed",
                    })
                    failed_step_number = step_order
                    for remaining in steps[idx + 1:]:
                        step_results.append({
                            "step_number": remaining["step_order"],
                            "original_description": remaining["description"],
                            "success": False,
                            "status": "skipped",
                            "thinking": "",
                            "action": "",
                            "next_goal": "",
                            "error": f"Skipped due to step {failed_step_number} failure",
                            "screenshot_path": None,
                            "duration_ms": 0,
                        })
                    break

                memory_mode = "read_write"
                try:
                    from app.runtime_config import healing_config as _hc
                    memory_enabled = bool(getattr(_hc, "locator_memory_enabled", True))
                    memory_mode = getattr(_hc, "locator_memory_mode", None) or (
                        "read_write" if memory_enabled else "off"
                    )
                    if not memory_enabled:
                        memory_mode = "off"
                except Exception:
                    memory_mode = "read_write"
                can_write_memory = memory_mode == "read_write" and cacheable

                tool_call = None
                result = None
                used_replay = False
                used_plan_replay = False
                invalidate = False
                failure_kind = None

                # 1b. Try learned plan/locator replay (skip LLM)
                if memory_mode != "off" and cacheable and cached_fp:
                    from core.locator_memory import try_replay_plan_mcp

                    class _Adapter:
                        def __init__(self, outer):
                            self._outer = outer

                        async def execute_tool_call(self, tc):
                            return await self._outer._execute_step(
                                session, agent_id, run_id, step_order, desc, tc,
                            )

                        async def get_dom_snapshot(self):
                            return await self._outer._get_snapshot(
                                session, agent_id, run_id,
                            )

                    replay = await try_replay_plan_mcp(
                        _Adapter(self),
                        cached_fp,
                        snapshot=snap or "",
                        step_description=desc,
                    )
                    if replay.get("success") and not replay.get("skipped"):
                        used_replay = True
                        used_plan_replay = bool(replay.get("plan_replay"))
                        tc_dict = replay.get("tool_call") or {}
                        from core.llm_wrapper import PlaywrightMCPToolCall
                        tool_call = PlaywrightMCPToolCall(
                            action=tc_dict.get("action") or "click",
                            selector=tc_dict.get("selector"),
                            value=tc_dict.get("value"),
                            thinking=tc_dict.get("thinking") or "locator_memory replay",
                            next_goal="",
                        )
                        result = replay.get("exec_result") or {"success": True}
                        result.setdefault(
                            "action",
                            f"{tool_call.action}({tool_call.selector})",
                        )
                    elif not replay.get("skipped"):
                        invalidate = True
                        failure_kind = "locator_miss"

                # 2. Two-phase Intent + bind; dropdown steps → open then pick
                if not used_replay:
                    from core.step_executor import (
                        dropdown_open_description,
                        dropdown_pick_description,
                        normalize_step_description,
                        option_choice_visible_in_snapshot,
                        parse_dropdown_select,
                    )

                    label, option = parse_dropdown_select(desc)
                    resolve_desc = normalize_step_description(desc)

                    async def _resolve_and_run(
                        step_desc: str,
                        snap_text: str,
                        expect,
                        struct=None,
                        prefer_selector: bool = True,
                    ):
                        from core.locator_memory import extract_page_url
                        from core.llm_wrapper import PlaywrightMCPToolCall
                        from core.step_intent import (
                            goto_is_redundant,
                            selector_tool_call_candidates,
                        )

                        class _SnapAdapter:
                            def __init__(self, outer):
                                self._outer = outer

                            async def get_dom_snapshot(self):
                                return await self._outer._get_snapshot(
                                    session, agent_id, run_id,
                                )

                            async def take_screenshot(self, *a, **k):
                                # vision may call screenshot APIs — best-effort via snapshot only
                                return None

                        # Skip goto that would leave deeper BASE/current path
                        if isinstance(struct, dict) and (
                            (struct.get("action") or "").strip().lower() == "goto"
                        ):
                            goto_url = struct.get("value")
                            current = extract_page_url(snap_text or "")
                            if goto_is_redundant(
                                current, goto_url, base_url_override,
                            ):
                                tc = PlaywrightMCPToolCall(
                                    action="goto",
                                    selector=None,
                                    value=goto_url,
                                    thinking=(
                                        f"skip redundant goto "
                                        f"(already at {current or base_url_override!r}; "
                                        f"target={goto_url!r})"
                                    ),
                                    next_goal="verify this step only",
                                )
                                logger.info(
                                    "Skip redundant goto current=%r target=%r base=%r",
                                    current, goto_url, base_url_override,
                                )
                                return tc, {
                                    "success": True,
                                    "action": f"goto({goto_url})",
                                    "error": None,
                                    "skipped_goto": True,
                                    "duration_ms": 0,
                                }

                        # Prefer solidified/derived CSS → execute; on fail → semantic
                        if prefer_selector:
                            for sel_tc in selector_tool_call_candidates(struct):
                                logger.info(
                                    "Trying solidified selector action=%s selector=%r",
                                    sel_tc.action, sel_tc.selector,
                                )
                                res = await self._execute_step(
                                    session, agent_id, run_id, step_order, desc,
                                    sel_tc.model_dump(),
                                )
                                if res.get("success"):
                                    return sel_tc, res
                                logger.info(
                                    "Solidified selector failed (%s); "
                                    "trying next selector or semantic bind",
                                    (res.get("error") or "")[:160],
                                )

                        tc = await _resolve_agent_tool_call(
                            desc=step_desc,
                            snap=snap_text or "",
                            expected_result=expect,
                            llm_client=llm_client,
                            model=model,
                            base_url_override=base_url_override,
                            structured_step=struct,
                            mcp_manager=_SnapAdapter(self),
                            prefer_selector=False,
                        )
                        act = (tc.action or "").lower()
                        if act in ("error", "done"):
                            return tc, {
                                "success": False if act == "error" else True,
                                "action": f"{tc.action}({tc.value or ''})",
                                "error": (
                                    tc.value or "LLM 无法确定本步操作"
                                    if act == "error"
                                    else None
                                ),
                                "duration_ms": 0,
                            }
                        res = await self._execute_step(
                            session, agent_id, run_id, step_order, desc, tc.model_dump(),
                        )
                        return tc, res

                    if label and option:
                        need_open = not option_choice_visible_in_snapshot(snap or "", option)
                        if need_open:
                            tool_call, result = await _resolve_and_run(
                                dropdown_open_description(label), snap or "", None,
                            )
                            if not result.get("success"):
                                # fall through to relocate below
                                pass
                            else:
                                snap = await self._get_snapshot(session, agent_id, run_id)
                                tool_call, result = await _resolve_and_run(
                                    dropdown_pick_description(option), snap or "", expected_result,
                                )
                        else:
                            tool_call, result = await _resolve_and_run(
                                dropdown_pick_description(option), snap or "", expected_result,
                            )
                    elif option:
                        # 点击【京州市院】（搜索结果中的）— wait for filter rows, verify selection
                        from core.step_executor import (
                            option_selected_in_snapshot,
                            wait_for_option_in_snapshot,
                        )

                        # Fill often finishes before filter rows paint — settle briefly
                        await asyncio.sleep(max(HYBRID_SETTLE_SECONDS, 0.5))
                        snap = await self._get_snapshot(session, agent_id, run_id)
                        if not option_choice_visible_in_snapshot(
                            snap or "",
                            option,
                            strong_only=True,
                            include_tree=True,
                            include_label_text=True,
                        ):
                            snap = await wait_for_option_in_snapshot(
                                lambda: self._get_snapshot(session, agent_id, run_id),
                                option,
                                timeout_s=4.0,
                                prefer_strong=True,
                                include_tree=True,
                            )
                        # Click option/treeitem/text — never tooltip (click-through → 山西省院)
                        if not option_choice_visible_in_snapshot(
                            snap or "",
                            option,
                            strong_only=True,
                            include_tree=True,
                            include_label_text=True,
                        ):
                            logger.info(
                                "search option: no non-tooltip AX hit for %r — "
                                "skip MCP tooltip click, fail fast to hybrid",
                                option,
                            )
                            tool_call = None
                            result = {
                                "success": False,
                                "error": (
                                    f"筛选结果「{option}」尚无 option/treeitem 等可点节点，"
                                    "跳过 tooltip 误点"
                                ),
                                "duration_ms": 0,
                            }
                        else:
                            pick_struct = {
                                "action": "click",
                                "target_name": option,
                                "disambiguation": "搜索结果中的",
                            }
                            tool_call, result = await _resolve_and_run(
                                dropdown_pick_description(option),
                                snap or "",
                                expected_result,
                                pick_struct,
                            )
                            if result.get("success"):
                                await asyncio.sleep(max(HYBRID_SETTLE_SECONDS, 0.5))
                                post = await self._get_snapshot(session, agent_id, run_id)
                                if not option_selected_in_snapshot(post or "", option):
                                    logger.warning(
                                        "option click did not stick: want=%r",
                                        option,
                                    )
                                    result = {
                                        **result,
                                        "success": False,
                                        "error": (
                                            f"选项「{option}」点击后单位未选中"
                                        ),
                                    }
                    else:
                        tool_call, result = await _resolve_and_run(
                            resolve_desc, snap or "", expected_result, structured_step,
                        )

                # Hybrid relocate: on error/MCP fail, refresh snapshot and retry once
                # Re-run Intent+bind on fresh snap (same as server) to avoid stale refs
                action_lower = (getattr(tool_call, "action", None) or "").lower() if tool_call else ""
                browser_closed = self._error_indicates_browser_closed(
                    str(result.get("error") or "")
                )
                if (
                    not used_replay
                    and not result.get("success")
                    and action_lower != "done"
                    and not browser_closed
                ):
                    logger.info(
                        "Hybrid relocate (agent): step %s failed; refreshing snapshot",
                        step_order,
                    )
                    await asyncio.sleep(HYBRID_SETTLE_SECONDS)
                    snap = await self._get_snapshot(session, agent_id, run_id)
                    from core.step_executor import (
                        dropdown_open_description,
                        dropdown_pick_description,
                        normalize_step_description,
                        option_choice_visible_in_snapshot,
                        parse_dropdown_select,
                    )
                    label, option = parse_dropdown_select(desc)
                    resolve_desc = normalize_step_description(desc)

                    async def _resolve_and_run_retry(step_desc: str, snap_text: str, expect, struct=None):
                        # Relocate: semantic bind only (selector already tried)
                        tc = await _resolve_agent_tool_call(
                            desc=step_desc,
                            snap=snap_text or "",
                            expected_result=expect,
                            llm_client=llm_client,
                            model=model,
                            base_url_override=base_url_override,
                            structured_step=struct,
                            prefer_selector=False,
                        )
                        act = (tc.action or "").lower()
                        if act in ("error", "done"):
                            return tc, {
                                "success": False if act == "error" else True,
                                "action": f"{tc.action}({tc.value or ''})",
                                "error": (
                                    tc.value or "LLM 无法确定本步操作"
                                    if act == "error"
                                    else None
                                ),
                                "duration_ms": 0,
                                "relocate_attempted": True,
                            }
                        res = await self._execute_step(
                            session, agent_id, run_id, step_order, desc, tc.model_dump(),
                        )
                        res["relocate_attempted"] = True
                        return tc, res

                    if label and option:
                        need_open = not option_choice_visible_in_snapshot(snap or "", option)
                        if need_open:
                            tool_call, result = await _resolve_and_run_retry(
                                dropdown_open_description(label), snap or "", None,
                            )
                            if result.get("success"):
                                snap = await self._get_snapshot(session, agent_id, run_id)
                                tool_call, result = await _resolve_and_run_retry(
                                    dropdown_pick_description(option),
                                    snap or "",
                                    expected_result,
                                )
                        else:
                            tool_call, result = await _resolve_and_run_retry(
                                dropdown_pick_description(option),
                                snap or "",
                                expected_result,
                            )
                    elif option:
                        from core.step_executor import (
                            option_selected_in_snapshot,
                            wait_for_option_in_snapshot,
                        )

                        await asyncio.sleep(max(HYBRID_SETTLE_SECONDS, 0.5))
                        snap = await self._get_snapshot(session, agent_id, run_id)
                        if not option_choice_visible_in_snapshot(
                            snap or "",
                            option,
                            strong_only=True,
                            include_tree=True,
                            include_label_text=True,
                        ):
                            snap = await wait_for_option_in_snapshot(
                                lambda: self._get_snapshot(session, agent_id, run_id),
                                option,
                                timeout_s=4.0,
                                prefer_strong=True,
                                include_tree=True,
                            )
                        if not option_choice_visible_in_snapshot(
                            snap or "",
                            option,
                            strong_only=True,
                            include_tree=True,
                            include_label_text=True,
                        ):
                            logger.info(
                                "search option (relocate): no non-tooltip AX hit for %r — "
                                "skip MCP tooltip click",
                                option,
                            )
                            tool_call = None
                            result = {
                                "success": False,
                                "error": (
                                    f"筛选结果「{option}」尚无 option/treeitem 等可点节点，"
                                    "跳过 tooltip 误点"
                                ),
                                "duration_ms": 0,
                                "relocate_attempted": True,
                            }
                        else:
                            pick_struct = {
                                "action": "click",
                                "target_name": option,
                                "disambiguation": "搜索结果中的",
                            }
                            tool_call, result = await _resolve_and_run_retry(
                                dropdown_pick_description(option),
                                snap or "",
                                expected_result,
                                pick_struct,
                            )
                            if result.get("success"):
                                await asyncio.sleep(max(HYBRID_SETTLE_SECONDS, 0.5))
                                post = await self._get_snapshot(session, agent_id, run_id)
                                if not option_selected_in_snapshot(post or "", option):
                                    logger.warning(
                                        "option click did not stick (relocate): want=%r",
                                        option,
                                    )
                                    result = {
                                        **result,
                                        "success": False,
                                        "error": f"选项「{option}」点击后单位未选中",
                                        "relocate_attempted": True,
                                    }
                    else:
                        tool_call, result = await _resolve_and_run_retry(
                            resolve_desc, snap or "", expected_result, structured_step,
                        )
                # Hybrid: locator failure → same-browser browser-use for this NL step only
                step_backend = "playwright_mcp"
                if _should_hybrid_browser_use_fallback(
                    hybrid=hybrid, result=result, action_lower=action_lower
                ):
                    err_s = str(result.get("error") or "")
                    logger.info(
                        "Hybrid browser-use fallback: step %s after MCP locator failure: %s",
                        step_order, err_s[:200],
                    )
                    # Option/search mis-bind: keep browser-use short — do not burn 20 turns
                    fb_max = min(
                        20, int(execution_backend_config.max_steps_per_nl or 20)
                    )
                    if (
                        "跳过 tooltip" in err_s
                        or "筛选结果" in err_s
                        or "单位未选中" in err_s
                    ):
                        fb_max = min(fb_max, 8)
                    fb = await self._execute_step_browser_use_fallback(
                        session,
                        agent_id,
                        run_id,
                        step_order=step_order,
                        description=desc,
                        expected_result=expected_result,
                        max_steps_per_nl=fb_max,
                    )
                    if fb.get("success"):
                        result = fb
                        step_backend = "browser_use_fallback"
                    else:
                        # Keep richer error; still mark attempted
                        result = {
                            **result,
                            **{k: v for k, v in fb.items() if v is not None},
                            "success": False,
                            "fallback_attempted": True,
                            "fallback_error": fb.get("error"),
                        }
                        step_backend = "browser_use_fallback"
                elif (
                    hybrid
                    and not result.get("success")
                    and action_lower != "done"
                ):
                    logger.info(
                        "Hybrid skip browser-use for step %s (not classified as locator failure): %s",
                        step_order,
                        (result.get("error") or result.get("action") or "")[:200],
                    )
                elif not hybrid and not result.get("success") and action_lower != "done":
                    logger.info(
                        "No browser-use fallback for step %s (selected_backend=%s hybrid_inactive); "
                        "set Settings→执行后端 to hybrid if desired. error=%s",
                        step_order,
                        selected,
                        (result.get("error") or "")[:160],
                    )

                # 4. Verify expected result if step succeeded (Skyvern-style when expected set)
                # Skip placeholder expectations like「步骤执行成功」— LLM often false-fails them.
                _exp_norm = (expected_result or "").strip()
                _trivial_expected = _exp_norm in (
                    "",
                    "步骤执行成功",
                    "执行成功",
                    "成功",
                    "ok",
                    "OK",
                    "pass",
                    "passed",
                )
                if result.get("success") and expected_result and not _trivial_expected:
                    try:
                        from core.verification_strategy import VerificationStrategy
                        from core.llm_wrapper import verify_expected_result

                        action_type = (getattr(tool_call, "action", None) or "") if tool_call else ""
                        if VerificationStrategy.should_verify(
                            action_type, result.get("error"), has_expected=True,
                        ):
                            post_snap = await self._get_snapshot(session, agent_id, run_id)
                            # Login/navigation often lands briefly on about:blank — wait & refresh
                            if self._snapshot_looks_blank(post_snap):
                                logger.info(
                                    "Post-step snapshot blank/about:blank; waiting for navigation "
                                    "(step %s)",
                                    step_order,
                                )
                                for _wait_i in range(4):
                                    await asyncio.sleep(1.0)
                                    post_snap = await self._get_snapshot(
                                        session, agent_id, run_id,
                                    )
                                    if not self._snapshot_looks_blank(post_snap):
                                        break
                            verification = await asyncio.wait_for(
                                verify_expected_result(
                                    expected_result, post_snap, client=llm_client, model=model,
                                ),
                                timeout=30,
                            )
                            if not verification.passed:
                                logger.info(
                                    "Hybrid assert retry (agent): step %s L2 failed; refreshing",
                                    step_order,
                                )
                                await asyncio.sleep(max(HYBRID_SETTLE_SECONDS, 1.5))
                                post_snap = await self._get_snapshot(session, agent_id, run_id)
                                if self._snapshot_looks_blank(post_snap):
                                    await asyncio.sleep(2.0)
                                    post_snap = await self._get_snapshot(
                                        session, agent_id, run_id,
                                    )
                                verification = await asyncio.wait_for(
                                    verify_expected_result(
                                        expected_result, post_snap, client=llm_client, model=model,
                                    ),
                                    timeout=30,
                                )
                                result["assert_retry_attempted"] = True
                                if not verification.passed:
                                    result["success"] = False
                                    result["error"] = (
                                        f"Expected result verification failed: {verification.reason}"
                                    )
                                    failure_kind = "semantic_drift"
                                    invalidate = True
                                else:
                                    result["verification"] = verification.reason
                            else:
                                result["verification"] = verification.reason
                    except asyncio.TimeoutError:
                        logger.warning(f"Step {step_order} verification timed out")
                    except Exception as exc:
                        logger.warning(f"Step {step_order} verification failed: {exc}")

                if not result.get("success") and failure_kind is None:
                    action_lower = (getattr(tool_call, "action", None) or "").lower() if tool_call else ""
                    if action_lower == "error":
                        failure_kind = "locator_miss"
                    else:
                        failure_kind = "action_fail"

                # 失败步骤必须截图（含 LLM error/done 短路、验证失败、超时等未带回截图的场景）
                if not result.get("success") and not result.get("screenshot_base64"):
                    try:
                        ss_result = await self._get_screenshot(session, agent_id, run_id)
                        if ss_result and ss_result.get("screenshot_base64"):
                            result["screenshot_base64"] = ss_result["screenshot_base64"]
                    except Exception as exc:
                        logger.warning(f"Failed to request failure screenshot for step {step_order}: {exc}")

                # Handle screenshot base64 from agent
                screenshot_path = None
                ss_b64 = result.get("screenshot_base64")
                if ss_b64 and output_dir:
                    try:
                        ss_dir = os.path.join(output_dir, "screenshots")
                        os.makedirs(ss_dir, exist_ok=True)
                        ss_path = os.path.join(ss_dir, f"step_{step_order}.png")
                        with open(ss_path, "wb") as f:
                            f.write(base64.b64decode(ss_b64))
                        # 报告/前端使用 /reports/... 相对路径
                        screenshot_path = ss_path.replace("\\", "/")
                        result.pop("screenshot_base64", None)
                    except Exception as exc:
                        logger.warning(f"Failed to save screenshot for step {step_order}: {exc}")

                step_result = {
                    "step_number": step_order,
                    "step_id": step.get("id"),
                    "original_description": desc,
                    "success": result.get("success", False),
                    "thinking": (tool_call.thinking if tool_call else "") or "",
                    "action": result.get("action", ""),
                    "next_goal": (tool_call.next_goal if tool_call else "") or "",
                    "error": result.get("error"),
                    "duration_ms": result.get("duration_ms", 0),
                    "screenshot_path": screenshot_path,
                    "verification": result.get("verification"),
                    "backend": step_backend,
                    "locator_replay": used_replay,
                    "plan_replay": used_plan_replay,
                    "failure_kind": failure_kind,
                    "learned_locator": None,
                    "invalidate_learned_locator": False,
                    "resolved_selector": (
                        tool_call.selector if tool_call and getattr(tool_call, "selector", None) else None
                    ),
                }

                if invalidate or (used_replay and not step_result["success"]):
                    step_result["invalidate_learned_locator"] = True
                elif (
                    step_result["success"]
                    and can_write_memory
                    and step_backend == "browser_use_fallback"
                    and isinstance(result.get("learned_locator"), dict)
                ):
                    # Solidify browser-use rescue actions for next MCP/hybrid run.
                    step_result["learned_locator"] = result["learned_locator"]
                elif step_result["success"] and can_write_memory and tool_call is not None:
                    from core.locator_memory import learn_fingerprint_after_success

                    fp = learn_fingerprint_after_success(
                        action=tool_call.action,
                        selector=tool_call.selector,
                        value=tool_call.value,
                        snapshot=snap or "",
                        structured_step=structured_step,
                        cached_fp=cached_fp,
                        used_replay=used_replay,
                    )
                    if fp:
                        step_result["learned_locator"] = fp

                step_results.append(step_result)

                log_msg = (
                    f"Step {step_order} "
                    f"{'✓' if step_result['success'] else '✗'}"
                )
                if step_result['error']:
                    log_msg += f" — {step_result['error']}"
                logger.info(log_msg)

                if not step_result["success"]:
                    consecutive_failures += 1
                else:
                    consecutive_failures = 0

                if consecutive_failures >= max_failures:
                    failed_step_number = step_order
                    logger.warning(f"Step {step_order} failed, skipping remaining steps")
                    # Add skipped entries for remaining steps
                    for remaining in steps[idx + 1:]:
                        step_results.append({
                            "step_number": remaining["step_order"],
                            "original_description": remaining["description"],
                            "success": False,
                            "status": "skipped",
                            "thinking": "",
                            "action": "",
                            "next_goal": "",
                        "error": f"Skipped due to step {failed_step_number} failure",
                            "screenshot_path": None,
                            "duration_ms": 0,
                        })
                    break

            # Notify agent of run end (skip when nested under nl_goal — parent owns lifecycle)
            if own_run_lifecycle:
                try:
                    await session.send(WSMessage(
                        type=WSMessageType.RUN_END, agent_id=agent_id, run_id=run_id,
                    ))
                except ConnectionError as exc:
                    logger.warning("RUN_END not sent (agent disconnected): %s", exc)

        except ConnectionError as exc:
            logger.error(
                "Agent %s disconnected mid-run %s: %s", agent_id, run_id, exc,
            )
            if not step_results:
                step_results.append({
                    "step_number": 1,
                    "original_description": case_name,
                    "success": False,
                    "thinking": "",
                    "action": "",
                    "next_goal": "",
                    "error": f"Agent disconnected: {exc}",
                    "screenshot_path": None,
                    "duration_ms": 0,
                })
            elif step_results and step_results[-1].get("success"):
                # Mark a trailing failure so the run is not reported as green
                step_results.append({
                    "step_number": (step_results[-1].get("step_number") or 0) + 1,
                    "original_description": "(agent disconnected)",
                    "success": False,
                    "thinking": "",
                    "action": "",
                    "next_goal": "",
                    "error": f"Agent disconnected: {exc}",
                    "screenshot_path": None,
                    "duration_ms": 0,
                })
            else:
                # Annotate last failed step if needed
                last = step_results[-1]
                if not last.get("error"):
                    last["error"] = f"Agent disconnected: {exc}"

        finally:
            if own_run_lifecycle:
                session.agent.status = AgentStatus.ONLINE

        return step_results

    async def _execute_on_agent_browser_use(
        self,
        agent_id: str,
        run_id: str,
        case_name: str,
        steps: List[dict],
        *,
        output_dir: Optional[str] = None,
        base_url_override: Optional[str] = None,
        max_steps_per_nl: int = 20,
        headless: bool = True,
        navigate_base_url: bool = True,
        batch_id: Optional[int] = None,
    ) -> list:
        """Client-local browser-use: server sends NL steps + LLM config, waits for RUN_COMPLETE."""
        if batch_id is not None:
            from app import execution_control
            await execution_control.wait_if_paused(batch_id)
            if execution_control.is_stopped(batch_id):
                return [
                    {
                        "step_number": s.get("step_order"),
                        "original_description": s.get("description"),
                        "success": False,
                        "status": "cancelled",
                        "thinking": "",
                        "action": "",
                        "next_goal": "",
                        "error": "用户停止执行",
                        "screenshot_path": None,
                        "duration_ms": 0,
                    }
                    for s in steps
                ]

        session = await self.get_session(agent_id)
        if not session:
            raise ValueError(f"Agent {agent_id} not connected")

        caps = session.agent.capabilities or []
        if "browser_use" not in caps:
            raise ValueError(
                f"Agent {agent_id} 未声明 browser_use 能力。"
                "请升级客户端并安装 browser-use 后重连。"
            )

        key, base, model = await _llm_resolve_config()
        session.agent.status = AgentStatus.BUSY
        # Generous timeout: open URL + each NL step may take many agent turns
        timeout = max(600.0, 120.0 * max(1, len(steps)) + 180.0)
        logger.info(
            "browser-use client run started run_id=%s agent=%s case=%r steps=%s "
            "max_steps_per_nl=%s headless=%s (progress via RUN_LOG)",
            run_id, agent_id, case_name, len(steps), max_steps_per_nl, headless,
        )
        try:
            payload = await session.request(
                WSMessage(
                    type=WSMessageType.RUN_START,
                    agent_id=agent_id,
                    run_id=run_id,
                    payload={
                        "case_id": run_id,
                        "case_name": case_name,
                        "steps": steps,
                        "base_url": (base_url_override or "") if navigate_base_url else "",
                        "backend": "browser_use",
                        "max_steps_per_nl": max_steps_per_nl,
                        "headless": headless,
                        "navigate_base_url": navigate_base_url,
                        "llm": {
                            "api_key": key,
                            "api_base": base,
                            "model": model,
                        },
                    },
                ),
                timeout=timeout,
            )
            steps_out = payload.get("steps") if isinstance(payload, dict) else None
            if not isinstance(steps_out, list):
                err = (payload or {}).get("error") if isinstance(payload, dict) else "invalid RUN_COMPLETE"
                raise RuntimeError(f"Agent browser-use 未返回步骤结果: {err}")
            status = (payload or {}).get("status") if isinstance(payload, dict) else None
            err = (payload or {}).get("error") if isinstance(payload, dict) else None
            logger.info(
                "browser-use client run %s finished status=%s steps=%s err=%s",
                run_id, status, len(steps_out), (str(err)[:200] if err else None),
            )
            # Empty steps + failed/error must not look like "all passed" to callers
            if (status in ("failed", "error") or err) and not steps_out:
                raise RuntimeError(
                    f"Agent browser-use 执行失败: {err or status or 'unknown'}"
                )
            from core.browser_use_exec import persist_step_screenshot_files
            return persist_step_screenshot_files(steps_out, output_dir)
        finally:
            try:
                await session.send(WSMessage(
                    type=WSMessageType.RUN_END, agent_id=agent_id, run_id=run_id,
                ))
            except Exception:
                logger.debug("RUN_END after browser-use failed", exc_info=True)
            session.agent.status = AgentStatus.ONLINE

    async def _get_snapshot(self, session: AgentSession, agent_id: str, run_id: str) -> str:
        text = ""
        for attempt in range(3):
            try:
                msg = WSMessage(type=WSMessageType.GET_SNAPSHOT, agent_id=agent_id, run_id=run_id)
                # Keep short: dead browser must not block the agent for minutes
                payload = await session.request(msg, timeout=25)
                text = payload.get("text", "")
                if self._snapshot_indicates_browser_closed(text):
                    return text
                if text and "(page not available)" not in text and "(snapshot unavailable)" not in text and "(snapshot timeout)" not in text and "(browser ready)" not in text:
                    return text
            except Exception as exc:
                logger.debug(f"Snapshot attempt {attempt + 1}/3 failed: {exc}")
                await asyncio.sleep(2)
        return text or "(snapshot unavailable)"

    @staticmethod
    def _snapshot_indicates_browser_closed(text: str | None) -> bool:
        t = text or ""
        low = t.lower()
        return (
            "(browser closed by user)" in low
            or "浏览器已关闭" in t
        )

    @staticmethod
    def _snapshot_looks_blank(text: str | None) -> bool:
        """True when snapshot is empty or still on about:blank (post-nav race)."""
        t = (text or "").strip()
        if not t:
            return True
        low = t.lower()
        if low in (
            "(empty page)",
            "(snapshot unavailable)",
            "(snapshot timeout)",
            "(page not available)",
            "(browser ready)",
        ):
            return True
        if "about:blank" in low and (
            "page url" in low or "pageurl" in low.replace(" ", "") or len(t) < 200
        ):
            return True
        return False

    @staticmethod
    def _error_indicates_browser_closed(err: str | None) -> bool:
        t = err or ""
        low = t.lower()
        return (
            "浏览器已关闭" in t
            or "browser closed by user" in low
            or "browser has been closed" in low
            or "target closed" in low
        )

    async def _get_screenshot(self, session: AgentSession, agent_id: str, run_id: str) -> dict:
        try:
            msg = WSMessage(type=WSMessageType.GET_SCREENSHOT, agent_id=agent_id, run_id=run_id)
            payload = await session.request(msg, timeout=30)
            return payload
        except Exception as exc:
            logger.debug(f"Screenshot request failed: {exc}")
            return {}

    async def _execute_step(self, session: AgentSession, agent_id: str, run_id: str,
                             step_order: int, description: str, tool_call: dict) -> dict:
        try:
            msg = WSMessage(
                type=WSMessageType.STEP_EXECUTE, agent_id=agent_id, run_id=run_id,
                payload={"step_order": step_order, "description": description, "tool_call": tool_call},
            )
            payload = await session.request(msg, timeout=90)
            return payload
        except asyncio.TimeoutError:
            return {"success": False, "error": "Step timeout", "action": "", "duration_ms": 0}
        except ConnectionError as exc:
            # Propagate so the run aborts cleanly instead of cascading ASGI send errors
            raise
        except Exception as exc:
            return {
                "success": False,
                "error": f"Step execute failed: {exc}",
                "action": "",
                "duration_ms": 0,
            }

    async def _execute_step_browser_use_fallback(
        self,
        session: AgentSession,
        agent_id: str,
        run_id: str,
        *,
        step_order: int,
        description: str,
        expected_result: Optional[str] = None,
        max_steps_per_nl: int = 20,
    ) -> dict:
        """Ask client to run one NL step via browser-use on the shared CDP browser."""
        try:
            key, base, model = await _llm_resolve_config()
            timeout = max(300.0, 25.0 * max(1, int(max_steps_per_nl)))
            payload = await session.request(
                WSMessage(
                    type=WSMessageType.STEP_BROWSER_USE,
                    agent_id=agent_id,
                    run_id=run_id,
                    payload={
                        "step_order": step_order,
                        "description": description,
                        "expected_result": expected_result,
                        "max_steps_per_nl": max_steps_per_nl,
                        "llm": {
                            "api_key": key,
                            "api_base": base,
                            "model": model,
                        },
                    },
                ),
                timeout=timeout,
            )
            if not isinstance(payload, dict):
                return {"success": False, "error": "invalid STEP_BROWSER_USE reply", "action": "browser_use_fallback"}
            return payload
        except asyncio.TimeoutError:
            return {
                "success": False,
                "error": "browser-use fallback timeout",
                "action": "browser_use_fallback",
                "duration_ms": 0,
            }
        except ConnectionError as exc:
            logger.warning(
                "browser-use fallback step %s aborted — agent disconnected: %s",
                step_order, exc,
            )
            return {
                "success": False,
                "error": f"Agent disconnected: {exc}",
                "action": "browser_use_fallback",
                "duration_ms": 0,
            }
        except Exception as exc:
            logger.exception("browser-use fallback step %s failed", step_order)
            return {
                "success": False,
                "error": str(exc),
                "action": "browser_use_fallback",
                "duration_ms": 0,
            }

    # ---- bridge: AI Agent observe/act 指令 ----

    async def send(self, agent_id: str, message: dict) -> None:
        """将 raw dict 包装为 WSMessage 并通过 Agent 会话发送"""
        session = await self.get_session(agent_id)
        if not session:
            raise ValueError(f"Agent {agent_id} not connected")
        msg_type = WSMessageType(message.get("type", "step_execute"))
        run_id = message.get("run_id", "")
        ws_msg = WSMessage(type=msg_type, agent_id=agent_id, run_id=run_id, payload=message)
        await session.send(ws_msg)

    async def _send_and_wait(self, agent_id: str, message: dict, timeout: int = 60) -> dict:
        """通过 WS 发送消息并等待 agent 响应，使用 _pending future 机制"""
        run_id = message.get("run_id", "")
        if not run_id:
            return {"success": False, "error": "No run_id"}
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        self._pending[run_id] = future
        try:
            await self.send(agent_id, message)
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(run_id, None)
            return {"success": False, "error": "Bridge timeout"}
        except Exception:
            self._pending.pop(run_id, None)
            raise

    def resolve_pending(self, run_id: str, result: dict) -> None:
        """解析 _pending 中与 run_id 匹配的 future"""
        fut = self._pending.pop(run_id, None)
        if fut and not fut.done():
            fut.set_result(result)

    async def send_observe(self, agent_id: str, run_id: str) -> dict:
        """通过 WS 向 Agent 发送 observe 指令，等待快照结果"""
        return await self._send_and_wait(agent_id, {
            "type": "step_execute",
            "run_id": run_id,
            "action": "observe",
            "timeout": 15000
        }, timeout=60)

    async def send_act(self, agent_id: str, run_id: str, tool_call: dict) -> dict:
        """通过 WS 向 Agent 发送操作指令"""
        return await self._send_and_wait(agent_id, {
            "type": "step_execute",
            "run_id": run_id,
            "action": tool_call.get("name", tool_call.get("tool")),
            "selector": tool_call.get("args", {}).get("selector"),
            "value": tool_call.get("args", {}).get("value"),
            "url": tool_call.get("args", {}).get("url"),
            "timeout": 120000
        }, timeout=120)


    # ---- cross-worker pending execution polling ----

    async def start_poller(self):
        """后台任务：轮询 DB 中待执行的 agent 任务（跨 worker）。"""
        while True:
            try:
                await self._poll_once()
            except Exception:
                pass
            await asyncio.sleep(2)

    async def _poll_once(self):
        """检查一次待执行队列，取回属于本 worker 的 agent 的任务。

        Per-agent serialization: skip agents whose run lock is held, claim the
        oldest pending row (ORDER BY id) so init cases created first run first,
        and await execution (via lock inside execute_on_agent / explicit lock for OTA).
        """
        from app.database import AsyncSessionLocal
        from app.db_models import AgentRun
        async with AsyncSessionLocal() as db:
            for agent_id in list(self.sessions.keys()):
                # Do not claim another job while this agent still has a run in flight
                if agent_id in self._agent_busy or self._run_lock_for(agent_id).locked():
                    logger.debug(
                        "Poller skip agent=%s (busy=%s lock=%s)",
                        agent_id,
                        agent_id in self._agent_busy,
                        self._run_lock_for(agent_id).locked(),
                    )
                    continue
                result = await db.execute(
                    text(
                        "SELECT id, goal FROM agent_runs "
                        "WHERE status='pending' AND goal->>'type'='client_exec' "
                        "AND (goal->>'agent_name')=:name "
                        "ORDER BY COALESCE((goal->>'seq')::int, id) ASC, id ASC LIMIT 1 "
                        "FOR UPDATE SKIP LOCKED"
                    ),
                    {"name": agent_id},
                )
                row = result.first()
                if not row:
                    continue
                run_id = str(row[0])
                goal = row[1] or {}
                case_id = goal.get("case_id")
                if not case_id:
                    continue
                run_row_id = row[0]
                await db.execute(
                    text("UPDATE agent_runs SET status='running' WHERE id=:id"),
                    {"id": row[0]},
                )
                await db.commit()
                logger.info(
                    "Poller picked up pending exec: run=%s case=%s agent=%s",
                    run_id, case_id, agent_id,
                )
                # Reserve agent before scheduling so the next poll cannot claim another job
                self._agent_busy.add(agent_id)

                async def _finish_busy(_aid=agent_id):
                    self._agent_busy.discard(_aid)

                # 检查是否有 AI Agent（AgentDefinition）配置 — 须显式 OTA skill
                try:
                    from app.crud import agent_definition as _cad
                    from core.agent_ota import should_use_ota_agent
                    _def = await _cad.get_active_by_type(db, "execution")
                    if should_use_ota_agent(_def):
                        from core.agent_bridge import AgentBridge
                        bridge = AgentBridge(self, db, _def)
                        _batch_id = goal.get("batch_id") if isinstance(goal, dict) else None

                        async def _run_ota(
                            _bridge=bridge,
                            _case_id=int(case_id),
                            _agent_id=agent_id,
                            _goal=goal,
                            _run_row_id=run_row_id,
                            _batch_id=_batch_id,
                        ):
                            try:
                                await _bridge.orchestrate(
                                    case_id=_case_id,
                                    agent_id=_agent_id,
                                    goal=_goal,
                                    existing_run_id=_run_row_id,
                                    existing_batch_id=_batch_id,
                                    environment_id=(
                                        _goal.get("environment_id")
                                        if isinstance(_goal, dict) else None
                                    ),
                                )
                            finally:
                                await _finish_busy(_agent_id)

                        asyncio.create_task(_run_ota())
                        continue
                except Exception as _abe:
                    logger.warning("Poller AgentBridge attempt failed, falling back: %s", _abe)

                # 回退：传统 execute_on_agent 路径（内部已按 agent 串行加锁）
                from app import crud
                tc = await crud.get_test_case(db, int(case_id))
                if not tc:
                    await _finish_busy(agent_id)
                    continue
                steps_db = await crud.get_steps_for_case(db, int(case_id))
                steps = [
                    {
                        "id": s.id,
                        "step_order": s.step_order,
                        "description": s.description,
                        "expected_result": getattr(s, "parsed_result", ""),
                        "learned_locator": getattr(s, "learned_locator", None)
                        if isinstance(getattr(s, "learned_locator", None), dict)
                        else None,
                        # Required for compiled_script hash match (same shape as client exec)
                        "structured_step": getattr(s, "structured_step", None)
                        if isinstance(getattr(s, "structured_step", None), dict)
                        else None,
                        "cacheable": bool(getattr(s, "cacheable", True)),
                    }
                    for s in steps_db
                ]
                env_id = goal.get("environment_id") if isinstance(goal, dict) else None
                base_url = None
                try:
                    if env_id:
                        from app.crud.environment import get_environment as _ge
                        _env = await _ge(db, int(env_id))
                        if _env and (_env.base_url or "").strip():
                            base_url = _env.base_url.strip()
                    if not base_url and getattr(tc, "project_id", None):
                        _proj = await crud.get_project(db, tc.project_id)
                        if _proj and (getattr(_proj, "base_url", None) or "").strip():
                            base_url = _proj.base_url.strip()
                except Exception:
                    logger.warning(
                        "Poller failed to resolve BASE URL for case %s",
                        case_id, exc_info=True,
                    )

                # Batch follow-ups keep browser session: only seq=0 (or no seq) navigates
                navigate = True
                if isinstance(goal, dict) and goal.get("batch_id") is not None:
                    _seq = goal.get("seq")
                    try:
                        navigate = _seq is None or int(_seq) == 0
                    except (TypeError, ValueError):
                        navigate = True

                async def _run_with_fallback(
                    _base_url=base_url,
                    _agent_id=agent_id,
                    _run_id=run_id,
                    _tc=tc,
                    _case_id=case_id,
                    _steps=steps,
                    _run_row_id=run_row_id,
                    _navigate=navigate,
                    _compiled_script=getattr(tc, "compiled_script", None),
                    _compiled_script_hash=getattr(tc, "compiled_script_hash", None),
                ):
                    try:
                        # manage_busy=False: poller holds _agent_busy until DB update finishes
                        self._last_compiled_script_failed = None
                        self._last_synthesized_script = None
                        result = await self.execute_on_agent(
                            _agent_id, _run_id,
                            _tc.name or f"Case #{_case_id}", _steps,
                            base_url_override=_base_url,
                            navigate_base_url=_navigate,
                            manage_busy=False,
                            case_id=int(_case_id),
                            compiled_script=_compiled_script,
                            compiled_script_hash=_compiled_script_hash,
                            case_description=getattr(_tc, "description", None),
                        )
                        try:
                            from core.locator_memory import persist_learned_locators_from_results
                            from core.compiled_script import (
                                clear_compiled_script,
                                persist_compiled_script,
                            )
                            async with AsyncSessionLocal() as _ldb:
                                from app import crud as _crud
                                _steps_db = await _crud.get_steps_for_case(_ldb, int(_case_id))
                                step_results = result if isinstance(result, list) else []
                                await persist_learned_locators_from_results(
                                    _ldb,
                                    step_results,
                                    steps_by_order={s.step_order: s for s in _steps_db},
                                )
                                _tc_fresh = await _crud.get_test_case(_ldb, int(_case_id))
                                failed_meta = getattr(self, "_last_compiled_script_failed", None)
                                if (
                                    failed_meta
                                    and _tc_fresh
                                    and failed_meta.get("case_id") == int(_case_id)
                                ):
                                    if clear_compiled_script(_tc_fresh):
                                        await _ldb.commit()
                                        logger.info(
                                            "cleared compiled_script after failed replay case=%s",
                                            _case_id,
                                        )
                                    self._last_compiled_script_failed = None
                                synth = getattr(self, "_last_synthesized_script", None)
                                if (
                                    _tc_fresh
                                    and isinstance(synth, dict)
                                    and synth.get("case_id") == int(_case_id)
                                    and synth.get("script")
                                    and synth.get("steps_hash")
                                ):
                                    persist_compiled_script(
                                        _tc_fresh,
                                        script=synth["script"],
                                        steps_hash=synth["steps_hash"],
                                    )
                                    await _ldb.commit()
                                    logger.info(
                                        "persisted LLM-synthesized compiled_script case=%s hash=%s",
                                        _case_id,
                                        str(synth["steps_hash"])[:12],
                                    )
                                    self._last_synthesized_script = None
                        except Exception:
                            logger.warning(
                                "poller persist learned_locator/compiled_script failed",
                                exc_info=True,
                            )
                        async with AsyncSessionLocal() as _edb:
                            await _edb.execute(
                                text(
                                    "UPDATE agent_runs SET status='completed', result=:res WHERE id=:id"
                                ),
                                {"res": json.dumps(result), "id": _run_row_id},
                            )
                            await _edb.commit()
                    except Exception as exc:
                        logger.error("Poller execute_on_agent failed: %s", exc)
                        async with AsyncSessionLocal() as _edb:
                            await _edb.execute(
                                text(
                                    "UPDATE agent_runs SET status='failed', error=:err WHERE id=:id"
                                ),
                                {"err": str(exc), "id": _run_row_id},
                            )
                            await _edb.commit()
                    finally:
                        await _finish_busy(_agent_id)

                asyncio.create_task(_run_with_fallback())


# Global singleton
agent_manager = AgentManager()
