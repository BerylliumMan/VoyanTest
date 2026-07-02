"""Agent manager — WebSocket session tracking and step-by-step execution coordination."""

import asyncio
import base64
import logging
import os
import sys
from datetime import datetime, timedelta
from app.tz import now as tz_now
from typing import Dict, List, Optional, Callable, Awaitable

_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, _project_root)

from agent.models import (
    AgentInfo, AgentStatus, AgentRegistration,
    WSMessage, WSMessageType,
    StepResultPayload, SnapshotPayload, RunCompletePayload,
)

from core.llm_wrapper import create_openai_client, generate_tool_call, _resolve_config as _llm_resolve_config

logger = logging.getLogger("agent.manager")


class AgentSession:
    """Holds WebSocket send callback and agent metadata for a connected agent."""

    def __init__(self, agent: AgentInfo, send_fn: Callable[[str], Awaitable[None]]):
        self.agent = agent
        self._send = send_fn
        self._pending: Dict[str, asyncio.Future] = {}

    async def send(self, msg: WSMessage):
        await self._send(msg.model_dump_json())

    async def request(self, msg: WSMessage) -> dict:
        """Send and wait for a reply with matching run_id."""
        key = msg.run_id
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[key] = fut
        await self.send(msg)
        try:
            return await asyncio.wait_for(fut, timeout=180)
        except asyncio.TimeoutError:
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

    # ---- session management ----

    def register(self, agent_id: str, info: AgentRegistration, send_fn) -> AgentInfo:
        agent = AgentInfo(
            id=agent_id or info.name,
            name=info.name,
            hostname=info.hostname,
            ip_address=info.ip_address,
            capabilities=info.capabilities,
            status=AgentStatus.ONLINE,
            last_seen=tz_now(),
        )
        self.sessions[agent.id] = AgentSession(agent, send_fn)
        logger.info(f"Agent registered: {agent.name} ({agent.id})")
        return agent

    def unregister(self, agent_id: str):
        self.sessions.pop(agent_id, None)
        logger.info(f"Agent unregistered: {agent_id}")

    def heartbeat(self, agent_id: str):
        if agent_id in self.sessions:
            self.sessions[agent_id].agent.last_seen = tz_now()

    def get_online_agents(self) -> List[AgentInfo]:
        now = tz_now()
        result = []
        for s in self.sessions.values():
            if s.agent.last_seen is None:
                continue
            if (now - s.agent.last_seen).total_seconds() < 120:
                result.append(s.agent)
        return result

    def get_session(self, agent_id: str) -> Optional[AgentSession]:
        return self.sessions.get(agent_id)

    # ---- recording (agent-side browser, server-side CDP capture) ----

    async def start_agent_recording(self, agent_id: str, url: str, headless: bool = False) -> str:
        """Ask an agent to start its browser and return a CDP WebSocket URL.

        Returns the CDP URL string that the server can connect to for recording.
        """
        session = self.sessions.get(agent_id)
        if not session:
            raise ValueError(f"Agent {agent_id} not connected")
        session.agent.status = AgentStatus.BUSY
        run_id = f"rec-{os.urandom(4).hex()}"
        payload = await session.request(WSMessage(
            type=WSMessageType.RECORDING_START, agent_id=agent_id,
            run_id=run_id,
            payload={"url": url, "headless": headless},
        ))
        cdp_url = (payload or {}).get("cdp_url")
        if not cdp_url:
            raise RuntimeError(f"Agent {agent_id} did not return a CDP URL")
        return cdp_url

    async def stop_agent_recording(self, agent_id: str) -> None:
        """Tell agent to stop recording (browser stays alive)."""
        session = self.sessions.get(agent_id)
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
        session.agent.status = AgentStatus.ONLINE

    # ---- step-by-step execution (server-side LLM, agent-side browser) ----

    async def execute_on_agent(self, agent_id: str, run_id: str,
                                case_name: str, steps: List[dict],
                                output_dir: Optional[str] = None) -> dict:
        """Execute all steps via the agent. Server handles LLM, agent handles browser.

        Returns a full step results list compatible with the existing report format.
        """
        session = self.sessions.get(agent_id)
        if not session:
            raise ValueError(f"Agent {agent_id} not connected")

        session.agent.status = AgentStatus.BUSY
        step_results = []
        consecutive_failures = 0
        max_failures = 1
        failed_step_number = None

        try:
            # Notify agent of run start
            await session.send(WSMessage(
                type=WSMessageType.RUN_START, agent_id=agent_id,
                run_id=run_id,
                payload={"case_id": run_id, "case_name": case_name,
                         "steps": steps},
            ))

            llm_client = await create_openai_client()
            _, _, model = await _llm_resolve_config()

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

                logger.info(f"--- Step {step_order}: {desc} ---")

                # 1. Get DOM snapshot from agent's browser
                snap = await self._get_snapshot(session, agent_id, run_id)

                # 2. LLM generates tool call from step description + snapshot + expected result
                tool_call = await generate_tool_call(desc, snap, expected_result=expected_result, client=llm_client, model=model)

                # 3. Send tool call to agent for execution
                result = await self._execute_step(
                    session, agent_id, run_id, step_order, desc, tool_call.model_dump(),
                )

                # 4. Verify expected result if step succeeded
                if result.get("success") and expected_result:
                    try:
                        post_snap = await self._get_snapshot(session, agent_id, run_id)
                        from core.llm_wrapper import verify_expected_result
                        verification = await asyncio.wait_for(
                            verify_expected_result(expected_result, post_snap, client=llm_client, model=model),
                            timeout=30,
                        )
                        if not verification.passed:
                            result["success"] = False
                            result["error"] = f"Expected result verification failed: {verification.reason}"
                            # Capture screenshot on verification failure
                            ss_result = await self._get_screenshot(session, agent_id, run_id)
                            if ss_result and ss_result.get("screenshot_base64"):
                                result["screenshot_base64"] = ss_result["screenshot_base64"]
                        else:
                            result["verification"] = verification.reason
                    except asyncio.TimeoutError:
                        logger.warning(f"Step {step_order} verification timed out")
                    except Exception as exc:
                        logger.warning(f"Step {step_order} verification failed: {exc}")

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
                        screenshot_path = ss_path
                        # Remove from result to keep report clean
                        result.pop("screenshot_base64", None)
                    except Exception as exc:
                        logger.warning(f"Failed to save screenshot for step {step_order}: {exc}")

                step_result = {
                    "step_number": step_order,
                    "original_description": desc,
                    "success": result.get("success", False),
                    "thinking": tool_call.thinking or "",
                    "action": result.get("action", ""),
                    "next_goal": tool_call.next_goal or "",
                    "error": result.get("error"),
                    "duration_ms": result.get("duration_ms", 0),
                    "screenshot_path": screenshot_path,
                    "verification": result.get("verification"),
                }
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


# Global singleton
agent_manager = AgentManager()
