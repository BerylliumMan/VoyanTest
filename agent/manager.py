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

logger = logging.getLogger("agent.manager")

_LOCATOR_FAIL_HINTS = (
    "not found",
    "no element",
    "no popup",
    "no modal",
    "no dialog",
    "no alert",
    "no matching",
    "locator",
    "timeout",
    "timed out",
    "unable to find",
    "unable to locate",
    "cannot find",
    "can't find",
    "could not find",
    "cannot proceed",
    "can't proceed",
    "strict mode violation",
    "waiting for",
    "does not exist",
    "isn't visible",
    "is not visible",
    "not visible",
    "找不到",
    "无法定位",
    "定位失败",
    "无法确定本步",
    "无法确定",
    "看不到",
    "不存在",
    "没有找到",
    "未找到",
    "element is not",
)


def _is_locator_failure(result: dict | None) -> bool:
    """True when MCP/LLM failure looks like element locating, not assertion."""
    if not result:
        return False
    err = f"{result.get('error') or ''} {result.get('action') or ''}"
    low = err.lower()
    if "verification failed" in low or "expected result verification" in low or "断言" in err:
        return False
    # LLM soft-fail: action=error(...) means it refused / cannot do the step
    action = (result.get("action") or "").strip().lower()
    if action == "error" or action.startswith("error("):
        return True
    if any(h in low for h in _LOCATOR_FAIL_HINTS):
        return True
    # "No xxx found" / "none ... found" — common LLM phrasing (≠ substring "not found")
    if "found" in low and (
        low.startswith("no ")
        or " no " in low
        or "none " in low
        or "没有" in err
        or "未找到" in err
    ):
        return True
    return False


def _should_hybrid_browser_use_fallback(
    *, hybrid: bool, result: dict | None, action_lower: str
) -> bool:
    if not hybrid or not result or result.get("success") or action_lower == "done":
        return False
    return _is_locator_failure(result)


class AgentSession:
    """Holds WebSocket send callback and agent metadata for a connected agent."""

    def __init__(self, agent: AgentInfo, send_fn: Callable[[str], Awaitable[None]]):
        self.agent = agent
        self._send = send_fn
        self._pending: Dict[str, asyncio.Future] = {}

    async def send(self, msg: WSMessage):
        await self._send(msg.model_dump_json())

    async def request(self, msg: WSMessage, timeout: float = 180) -> dict:
        """Send and wait for a reply with matching run_id."""
        key = msg.run_id
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[key] = fut
        await self.send(msg)
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
                                manage_busy: bool = True) -> dict:
        """Execute all steps via the agent. Server handles LLM, agent handles browser.

        Serialized per agent so batch/init cases never overlap on the same browser.
        Returns a full step results list compatible with the existing report format.

        ``backend``: ``playwright_mcp`` (default) or ``browser_use`` (client-local NL agent).
        ``navigate_base_url``: when False, skip BASE URL navigation (batch follow-up cases).
        ``manage_busy``: when False, caller owns ``_agent_busy`` for the whole job lifecycle.
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
    ) -> dict:
        """Inner implementation; caller must hold ``_run_lock_for(agent_id)``."""
        from app.runtime_config import execution_backend_config

        selected = (backend or execution_backend_config.backend or "playwright_mcp").strip()
        if selected == "browser_use":
            return await self._execute_on_agent_browser_use(
                agent_id, run_id, case_name, steps,
                output_dir=output_dir,
                base_url_override=base_url_override if navigate_base_url else None,
                max_steps_per_nl=execution_backend_config.max_steps_per_nl,
                headless=execution_backend_config.headless,
                navigate_base_url=navigate_base_url,
            )

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
            "Client agent run backend selected=%s hybrid=%s agent=%s caps=%s",
            selected,
            hybrid,
            agent_id,
            session.agent.capabilities or [],
        )

        session.agent.status = AgentStatus.BUSY
        step_results = []
        consecutive_failures = 0
        max_failures = 1
        failed_step_number = None

        try:
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
                    },
                ),
                timeout=90,
            )
            if isinstance(ready, dict) and ready.get("ready") is False:
                err = ready.get("error") or ready.get("message") or ready.get("text") or "browser not ready"
                logger.error("Agent %s browser failed to start: %s", agent_id, err)
                raise RuntimeError(f"Agent browser failed to start: {err}")
            if isinstance(ready, dict) and ready.get("message") and "MCP start failed" in str(ready.get("message")):
                raise RuntimeError(str(ready.get("message")))

            llm_client = await create_openai_client()
            _, _, model = await _llm_resolve_config()

            # 等待浏览器就绪；仅首个用例（或显式要求时）导航 BASE URL
            await self._get_snapshot(session, agent_id, run_id)
            if navigate_base_url and base_url_override:
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
                    logger.warning(
                        "BASE URL Playwright navigation failed: %s",
                        nav_result.get("error") or "unknown",
                    )
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

                logger.info(f"--- Step {step_order}: {desc} ---")

                # 1. Get DOM snapshot from agent's browser
                snap = await self._get_snapshot(session, agent_id, run_id)

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

                # 2. Two-phase Intent + bind (Midscene-style); fall back to legacy on ambiguity
                if not used_replay:
                    from core.step_intent import (
                        generate_step_intent,
                        intent_to_tool_call,
                        match_intent_candidates,
                    )
                    try:
                        intent = await generate_step_intent(
                            desc, snap, expected_result=expected_result,
                            client=llm_client, model=model,
                        )
                        action_i = (intent.action or "").lower()
                        if action_i in ("wait", "assert_text", "goto", "press_key", "error"):
                            tool_call = intent_to_tool_call(intent, ref=None)
                        else:
                            cands = match_intent_candidates(snap, intent)
                            if len(cands) == 1:
                                tool_call = intent_to_tool_call(
                                    intent, ref=cands[0]["ref"],
                                )
                            else:
                                # Ambiguous / missing: one-shot with refs (Agent path has no vision MCP)
                                tool_call = await generate_tool_call(
                                    desc, snap, expected_result=expected_result,
                                    client=llm_client, model=model,
                                    base_url=base_url_override or None,
                                )
                    except Exception:
                        tool_call = await generate_tool_call(
                            desc, snap, expected_result=expected_result,
                            client=llm_client, model=model,
                            base_url=base_url_override or None,
                        )

                    # error / done 是 LLM 控制信号，不能当 Playwright 工具下发
                    action_lower = (tool_call.action or "").lower()
                    if action_lower in ("error", "done"):
                        result = {
                            "success": False if action_lower == "error" else True,
                            "action": f"{tool_call.action}({tool_call.value or ''})",
                            "error": (
                                tool_call.value or "LLM 无法确定本步操作"
                                if action_lower == "error"
                                else None
                            ),
                            "duration_ms": 0,
                        }
                    else:
                        # 3. Send tool call to agent for execution
                        result = await self._execute_step(
                            session, agent_id, run_id, step_order, desc, tool_call.model_dump(),
                        )

                # Hybrid relocate: on error/MCP fail, refresh snapshot and retry once
                action_lower = (getattr(tool_call, "action", None) or "").lower() if tool_call else ""
                if (
                    not used_replay
                    and not result.get("success")
                    and action_lower != "done"
                ):
                    logger.info(
                        "Hybrid relocate (agent): step %s failed; refreshing snapshot",
                        step_order,
                    )
                    await asyncio.sleep(HYBRID_SETTLE_SECONDS)
                    snap = await self._get_snapshot(session, agent_id, run_id)
                    tool_call = await generate_tool_call(
                        desc, snap,
                        expected_result=expected_result,
                        client=llm_client,
                        model=model,
                        base_url=base_url_override or None,
                    )
                    action_lower = (tool_call.action or "").lower()
                    if action_lower in ("error", "done"):
                        result = {
                            "success": False if action_lower == "error" else True,
                            "action": f"{tool_call.action}({tool_call.value or ''})",
                            "error": (
                                tool_call.value or "LLM 无法确定本步操作"
                                if action_lower == "error"
                                else None
                            ),
                            "duration_ms": 0,
                            "relocate_attempted": True,
                        }
                    else:
                        result = await self._execute_step(
                            session, agent_id, run_id, step_order, desc, tool_call.model_dump(),
                        )
                        result["relocate_attempted"] = True

                # Hybrid: locator failure → same-browser browser-use for this NL step only
                step_backend = "playwright_mcp"
                if _should_hybrid_browser_use_fallback(
                    hybrid=hybrid, result=result, action_lower=action_lower
                ):
                    logger.info(
                        "Hybrid browser-use fallback: step %s after MCP locator failure: %s",
                        step_order, (result.get("error") or "")[:200],
                    )
                    fb = await self._execute_step_browser_use_fallback(
                        session,
                        agent_id,
                        run_id,
                        step_order=step_order,
                        description=desc,
                        expected_result=expected_result,
                        max_steps_per_nl=min(
                            20, int(execution_backend_config.max_steps_per_nl or 20)
                        ),
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
                if result.get("success") and expected_result:
                    try:
                        from core.verification_strategy import VerificationStrategy
                        from core.llm_wrapper import verify_expected_result

                        action_type = (getattr(tool_call, "action", None) or "") if tool_call else ""
                        if VerificationStrategy.should_verify(
                            action_type, result.get("error"), has_expected=True,
                        ):
                            post_snap = await self._get_snapshot(session, agent_id, run_id)
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
                                await asyncio.sleep(HYBRID_SETTLE_SECONDS)
                                post_snap = await self._get_snapshot(session, agent_id, run_id)
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
                }

                if invalidate or (used_replay and not step_result["success"]):
                    step_result["invalidate_learned_locator"] = True
                elif step_result["success"] and can_write_memory and tool_call is not None:
                    from core.locator_memory import (
                        bump_hit_count,
                        extract_from_snapshot,
                        is_learnable_action,
                    )
                    action = tool_call.action
                    selector = tool_call.selector
                    if is_learnable_action(action) and selector:
                        fp = extract_from_snapshot(
                            snap or "",
                            str(selector),
                            action=str(action),
                            value=tool_call.value,
                        )
                        if fp:
                            if used_replay and cached_fp:
                                step_result["learned_locator"] = bump_hit_count(cached_fp)
                            else:
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

            # Notify agent of run end
            await session.send(WSMessage(
                type=WSMessageType.RUN_END, agent_id=agent_id, run_id=run_id,
            ))

        finally:
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
    ) -> list:
        """Client-local browser-use: server sends NL steps + LLM config, waits for RUN_COMPLETE."""
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
                payload = await session.request(msg)
                text = payload.get("text", "")
                if text and "(page not available)" not in text and "(snapshot unavailable)" not in text:
                    return text
            except Exception as exc:
                logger.debug(f"Snapshot attempt {attempt + 1}/3 failed: {exc}")
                await asyncio.sleep(3)
        return text or "(snapshot unavailable)"

    async def _get_screenshot(self, session: AgentSession, agent_id: str, run_id: str) -> dict:
        try:
            msg = WSMessage(type=WSMessageType.GET_SCREENSHOT, agent_id=agent_id, run_id=run_id)
            payload = await session.request(msg)
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
            payload = await session.request(msg)
            return payload
        except asyncio.TimeoutError:
            return {"success": False, "error": "Step timeout", "action": "", "duration_ms": 0}

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
                ):
                    try:
                        # manage_busy=False: poller holds _agent_busy until DB update finishes
                        result = await self.execute_on_agent(
                            _agent_id, _run_id,
                            _tc.name or f"Case #{_case_id}", _steps,
                            base_url_override=_base_url,
                            navigate_base_url=_navigate,
                            manage_busy=False,
                        )
                        try:
                            from core.locator_memory import persist_learned_locators_from_results
                            async with AsyncSessionLocal() as _ldb:
                                from app import crud as _crud
                                _steps_db = await _crud.get_steps_for_case(_ldb, int(_case_id))
                                await persist_learned_locators_from_results(
                                    _ldb,
                                    result if isinstance(result, list) else [],
                                    steps_by_order={s.step_order: s for s in _steps_db},
                                )
                        except Exception:
                            logger.warning("poller persist learned_locator failed", exc_info=True)
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
