"""Agent manager — WebSocket session tracking and OTA/AgentBridge coordination."""

import asyncio
import json
import logging
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from app.tz import now as tz_now
from typing import Dict, List, Optional, Callable, Awaitable
from sqlalchemy import text

_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, _project_root)

from agent.models import (
    AgentInfo, AgentStatus, AgentRegistration,
    WSMessage, WSMessageType,
    ApiRequestPayload, CAP_API_TEST,
)

from core.api_runner.variables import mask_headers

logger = logging.getLogger("agent.manager")


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
    """Manages connected agent WebSocket sessions and OTA/AgentBridge execution."""

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


    # ---- compiled_script replay (OTA-only; prefer AgentBridge for client_exec) ----

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
                                case_description: Optional[str] = None,
                                reuse_browser_session: bool = False) -> dict:
        """Thin wrapper: compiled_script replay only.

        NL goal / hybrid / browser_use paths are removed. Prefer AgentBridge for
        client_exec. Raises RuntimeError with "use AgentBridge / OTA only" when
        there is no runnable compiled_script.
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
                    reuse_browser_session=reuse_browser_session,
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
        reuse_browser_session: bool = False,
    ) -> dict:
        """Inner implementation; caller must hold ``_run_lock_for(agent_id)``.

        Only runs ``_try_run_compiled_script``. All other backends must use AgentBridge.
        """
        from app.runtime_config import execution_backend_config
        from core.compiled_script import steps_content_hash

        self._last_action_journal = None
        self._last_synthesized_script = None

        script = (compiled_script or "").strip()
        if not script:
            raise RuntimeError(
                "use AgentBridge / OTA only — execute_on_agent requires compiled_script "
                "(nl_goal/legacy paths removed)"
            )

        current_hash = steps_content_hash(steps)
        if compiled_script_hash and compiled_script_hash != current_hash:
            raise RuntimeError(
                "use AgentBridge / OTA only — compiled_script hash mismatch "
                f"(stored={compiled_script_hash[:12]} current={current_hash[:12]})"
            )

        from core.script_synthesize import check_script_covers_intents
        missing_cov = check_script_covers_intents(script, steps)
        if missing_cov:
            raise RuntimeError(
                "use AgentBridge / OTA only — compiled_script fails coverage "
                f"{missing_cov}"
            )

        py_results = await self._try_run_compiled_script(
            agent_id, run_id, case_name, steps,
            script=script,
            base_url=base_url_override,
            case_id=case_id,
            steps_hash=compiled_script_hash or current_hash,
            headless=None,
            keep_browser=bool(reuse_browser_session)
            or not navigate_base_url
            or bool(getattr(execution_backend_config, "keep_browser_after_run", True)),
        )
        if py_results is None:
            raise RuntimeError(
                "use AgentBridge / OTA only — compiled_script unsupported or timeout"
            )
        failed = any(r.get("compiled_script_failed") for r in py_results)
        if failed:
            self._last_compiled_script_failed = {
                "case_id": case_id,
                "error": next(
                    (r.get("error") for r in py_results if r.get("error")),
                    "compiled_script failed",
                ),
            }
        return py_results


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
        headless: Optional[bool] = None,
        keep_browser: bool = False,
    ) -> Optional[list]:
        """Ask agent to run solidified Playwright script. None = fall back.

        ``headless``:
          - ``None``: agent uses its own GUI/CLI setting (user-facing runs → usually headed)
          - ``True``/``False``: force mode (e.g. headed/headless override)
        ``keep_browser``: batch/init→main — run on shared CDP and do not close Chromium.
        """
        session = await self.get_session(agent_id)
        if not session:
            return None
        try:
            session.agent.status = AgentStatus.BUSY
            logger.info(
                "Trying compiled_script case=%s agent=%s hash=%s headless=%s keep_browser=%s",
                case_id, agent_id, (steps_hash or "")[:12], headless, keep_browser,
            )
            from app.runtime_config import execution_backend_config

            cfg = execution_backend_config
            payload = {
                "script": script,
                "base_url": base_url or "",
                "case_id": case_id or 0,
                "steps_hash": steps_hash,
                "case_name": case_name,
                "keep_browser": bool(keep_browser),
                "enhance": {
                    "trace_on_fail": bool(
                        getattr(cfg, "compiled_trace_on_fail", True)
                    ),
                    "native_dialog": bool(
                        getattr(cfg, "compiled_native_dialog", True)
                    ),
                    "dialog_policy": getattr(
                        cfg, "compiled_dialog_policy", "accept"
                    )
                    or "accept",
                    # Batch / shared session: never whole-case retry (side effects).
                    "settle_retry": (
                        0
                        if keep_browser
                        else int(getattr(cfg, "compiled_settle_retry", 1) or 0)
                    ),
                    "settle_ms": int(
                        getattr(cfg, "compiled_settle_ms", 800) or 800
                    ),
                },
            }
            if headless is not None:
                payload["headless"] = bool(headless)
            resp = await session.request(WSMessage(
                type=WSMessageType.RUN_COMPILED_SCRIPT,
                agent_id=agent_id,
                run_id=run_id,
                payload=payload,
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
            trace_path = resp.get("trace_path")
            logger.warning(
                "compiled_script run failed: %s%s",
                " ".join(str(resp.get("error") or "").split())[:180],
                f" trace={trace_path}" if trace_path else "",
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
                    "trace_path": trace_path,
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

    # ---- API test: client-side HTTP execution (029-api-testing contract §4) ----

    async def request_api_call(
        self,
        agent_name: str,
        payload: dict,
        timeout: float = 60.0,
    ) -> dict:
        """派发一条 ``api_request`` 并等待同 run_id 的 ``api_response``。

        返回 dict 形状与 ``api_response.payload`` 一致；所有失败路径都返回
        ``{"success": False, "error": ...}`` 而不是抛异常，便于调用方（T050）
        回退服务端执行。
        """
        session = await self.get_session(agent_name)
        if not session:
            return {
                "success": False,
                "agent_missing": True,
                "error": f"Agent 不存在或离线: {agent_name}",
            }
        # 能力协商（T049）：未声明 CAP_API_TEST 的客户端不认识 api_request
        # （老客户端会忽略该消息），必须在此短路——否则只会白等到超时。
        if not supports_api_test(session.agent.capabilities):
            reason = (
                f"Agent {agent_name} 未声明 api_test 能力，跳过客户端执行"
                "（请升级 Agent 客户端后重连；调用方可回退服务端执行）"
            )
            logger.warning("api_request skipped — %s", reason)
            return {
                "success": False,
                "unsupported": True,
                "capability_missing": True,
                "error": reason,
            }

        try:
            request_payload = ApiRequestPayload(**payload).model_dump()
        except Exception as exc:
            return {"success": False, "error": f"api_request payload 非法: {exc}"}

        run_id = uuid.uuid4().hex
        logger.info(
            "api_request run_id=%s agent=%s %s %s timeout_ms=%s "
            "follow_redirects=%s verify_ssl=%s headers=%s",
            run_id,
            agent_name,
            request_payload["method"],
            request_payload["url"],
            request_payload["timeout_ms"],
            request_payload["follow_redirects"],
            request_payload["verify_ssl"],
            mask_headers(request_payload.get("headers") or {}),
        )
        try:
            resp = await session.request(
                WSMessage(
                    type=WSMessageType.API_REQUEST,
                    agent_id=agent_name,
                    run_id=run_id,
                    payload=request_payload,
                ),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            logger.warning("api_request timeout run_id=%s agent=%s", run_id, agent_name)
            return {"success": False, "error": "客户端执行超时"}
        except Exception as exc:
            logger.warning(
                "api_request dispatch failed run_id=%s agent=%s: %s",
                run_id, agent_name, exc,
            )
            return {"success": False, "error": f"客户端执行失败: {exc}"}

        if not isinstance(resp, dict):
            return {
                "success": False,
                "error": f"Agent 返回非法 api_response: {type(resp).__name__}",
            }
        logger.info(
            "api_response run_id=%s agent=%s success=%s status=%s "
            "duration_ms=%s size=%s truncated=%s",
            run_id,
            agent_name,
            resp.get("success"),
            resp.get("status"),
            resp.get("duration_ms"),
            resp.get("size"),
            resp.get("truncated"),
        )
        return resp


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

    async def send_observe(self, agent_id: str, run_id: str, want_screenshot: bool = False) -> dict:
        """通过 WS 向 Agent 发送 observe 指令，等待快照结果。

        截图按需开启（失败轮 / overlay / canvas）；成功常规轮默认关以省 token。
        """
        return await self._send_and_wait(agent_id, {
            "type": "step_execute",
            "run_id": run_id,
            "action": "observe",
            "want_screenshot": want_screenshot,
            "timeout": 15000
        }, timeout=60)

    async def send_act(self, agent_id: str, run_id: str, tool_call: dict) -> dict:
        """通过 WS 向 Agent 发送操作指令（025-ref-click: 全字段透传）"""
        args = tool_call.get("args", {})
        return await self._send_and_wait(agent_id, {
            "type": "step_execute",
            "run_id": run_id,
            "action": tool_call.get("name", tool_call.get("tool")),
            "selector": args.get("selector"),
            "element_desc": args.get("element_desc") or args.get("element"),
            "description": tool_call.get("description") or args.get("description"),
            "value": args.get("value"),
            "url": args.get("url"),
            "timeout_ms": args.get("timeout_ms", 30000),
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
        and await execution via AgentBridge (OTA only; no nl_goal/legacy fallback).
        """
        from app.database import AsyncSessionLocal
        async with AsyncSessionLocal() as db:
            # ── 026-gen-exec-fixes: 过期 pending run TTL 清理 ──
            try:
                stale = (await db.execute(
                    text(
                        "SELECT id, goal FROM agent_runs "
                        "WHERE status='pending' AND created_at < :cutoff "
                        "ORDER BY id ASC LIMIT 50"
                    ),
                    {
                        "cutoff": datetime.now(timezone.utc)
                        - timedelta(minutes=pending_ttl_minutes())
                    },
                )).fetchall()
                for _row in stale:
                    if isinstance(_row.goal, dict):
                        _goal = _row.goal
                    else:
                        try:
                            _goal = json.loads(_row.goal or "{}")
                        except Exception:
                            _goal = {}
                            logger.warning("TTL 清理: run=%s goal 解析失败", _row.id)
                    await db.execute(
                        text(
                            "UPDATE agent_runs SET status='failed', "
                            "error=:err WHERE id=:rid AND status='pending'"
                        ),
                        {
                            "rid": _row.id,
                            "err": f"等待客户端接单超时(TTL {pending_ttl_minutes()}min)",
                        },
                    )
                    _bid = _goal.get("batch_id")
                    logger.info("TTL 清理 run=%s goal_type=%s batch_id=%s", _row.id, type(_goal).__name__, _bid)
                    if _bid:
                        _bres = await db.execute(
                            text("UPDATE run_batches SET failed=COALESCE(failed,0)+1, status="
                                 "CASE WHEN status='running' THEN 'failed' ELSE status END "
                                 "WHERE id=:bid"),
                            {"bid": _bid},
                        )
                        logger.warning("batch %s 修正 rowcount=%s", _bid, _bres.rowcount)
                    logger.warning(
                        "Pending run #%s 超过 TTL %smin，已置 failed",
                        _row.id, pending_ttl_minutes(),
                    )
                if stale:
                    await db.commit()
            except Exception:
                await db.rollback()
                logger.warning("Pending TTL 清理失败（继续正常轮询）", exc_info=True)

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
                        _batch_id = goal.get("batch_id") if isinstance(goal, dict) else None
                        _def_id = _def.id

                        async def _run_ota(
                            _def_id=_def_id,
                            _fallback_def=_def,
                            _case_id=int(case_id),
                            _agent_id=agent_id,
                            _goal=goal,
                            _run_row_id=run_row_id,
                            _batch_id=_batch_id,
                        ):
                            from app.database import AsyncSessionLocal as _ota_session
                            try:
                                async with _ota_session() as _ota_db:
                                    _ota_def = await _cad.get_agent_definition(
                                        _ota_db, _def_id
                                    )
                                    _bridge = AgentBridge(
                                        self, _ota_db, _ota_def or _fallback_def
                                    )
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
                                        notify_user_id=(
                                            _goal.get("user_id")
                                            if isinstance(_goal, dict) else None
                                        ),
                                    )
                            finally:
                                await _finish_busy(_agent_id)

                        asyncio.create_task(_run_ota())
                        continue

                except Exception as _abe:
                    logger.warning(
                        "Poller AgentBridge attempt failed: %s", _abe,
                    )
                    _fail_err = (
                        f"use AgentBridge / OTA only — AgentBridge setup failed: {_abe}"
                    )
                else:
                    _fail_err = (
                        "use AgentBridge / OTA only — no active OTA execution agent "
                        "with tools ready (legacy nl_goal/hybrid paths removed)"
                    )

                # No OTA path taken — fail pending run; never call nl_goal/legacy
                async def _fail_ota_only(
                    _agent_id=agent_id,
                    _run_row_id=run_row_id,
                    _err=_fail_err,
                ):
                    try:
                        async with AsyncSessionLocal() as _edb:
                            await _edb.execute(
                                text(
                                    "UPDATE agent_runs SET status='failed', "
                                    "error=:err WHERE id=:id"
                                ),
                                {"err": _err, "id": _run_row_id},
                            )
                            await _edb.commit()
                    except Exception:
                        logger.exception(
                            "Poller failed to mark run failed after OTA miss"
                        )
                    finally:
                        await _finish_busy(_agent_id)

                logger.error(
                    "Poller rejecting client_exec run=%s — %s",
                    run_id, _fail_err,
                )
                asyncio.create_task(_fail_ota_only())



# Global singleton
agent_manager = AgentManager()


# ── 026-gen-exec-fixes: pending run TTL ─────────────────────────────────────

def pending_ttl_minutes() -> int:
    import os
    try:
        return max(1, int(os.getenv("PENDING_RUN_TTL_MIN", "30")))
    except (TypeError, ValueError):
        return 30


def is_pending_expired(created_at, now=None) -> bool:
    """pending run 是否超过 TTL；naive datetime 按 UTC 处理。"""
    if created_at is None:
        return False
    from datetime import datetime, timezone
    now = now or datetime.now(timezone.utc)
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    ttl = timedelta(minutes=pending_ttl_minutes())
    return (now - created_at) > ttl


def supports_compiled_script(capabilities) -> bool:
    """客户端是否声明 compiled_script 能力；无能力列表一律视为不支持。"""
    if not capabilities:
        return False
    return "compiled_script" in capabilities


def supports_api_test(capabilities) -> bool:
    """客户端是否声明 CAP_API_TEST 能力；无能力列表一律视为不支持。

    与 WSMessageType.API_REQUEST / API_RESPONSE 一一对应（见 agent/models.py）：
    服务端只向声明了该能力的客户端派发 api_request。
    """
    if not capabilities:
        return False
    return CAP_API_TEST in capabilities
