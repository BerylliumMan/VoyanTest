"""036+：脚本执行共享入口（对话 run_script 工具 / 执行链 run_script 动作共用）。

- 沙箱口径与约束不变（core/script_sandbox.py：白名单语法 / for 有界 / 超时 / 64KB 上限）；
- SDK 命名空间（app/services/script_sdk.py）：http / db（只读）/ platform（双层权限）/ files（白名单）；
- 门控由调用方检查（对话=chat_script_enabled；执行链=exec_script_enabled）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_MS = 30000
OUTPUT_EXCERPT_CHARS = 12000


async def execute_script(
    *,
    user_id: Optional[int],
    script: str,
    title: str = "",
    timeout_ms: Optional[int] = None,
) -> dict[str, Any]:
    """在受控沙箱中执行脚本；返回 {ok, output, error, duration_ms, db_queries, http_calls, truncated, title}。"""
    text = str(script or "")
    if not text.strip():
        return {
            "ok": False, "output": "", "error": "script 不能为空", "duration_ms": 0,
            "db_queries": 0, "http_calls": 0, "truncated": False,
            "title": (title or "").strip() or None,
        }
    try:
        from app.config import get_settings

        timeout_ms = int(timeout_ms or getattr(get_settings(), "chat_script_timeout_ms", DEFAULT_TIMEOUT_MS) or DEFAULT_TIMEOUT_MS)
    except Exception:
        timeout_ms = int(timeout_ms or DEFAULT_TIMEOUT_MS)
    timeout_ms = max(1000, min(int(timeout_ms), 120000))

    from app.database import AsyncSessionLocal
    from app.services.script_sdk import SdkState, build_sdk
    from core.script_sandbox import run_script

    loop = asyncio.get_running_loop()
    state = SdkState()

    def _run_async(coro: Any) -> Any:
        fut = asyncio.run_coroutine_threadsafe(coro, loop)
        return fut.result(timeout=(timeout_ms / 1000) + 5)

    sdk = build_sdk(
        run_async=_run_async,
        db_factory=AsyncSessionLocal,
        user_id=user_id,
        state=state,
    )

    t0 = time.monotonic()
    result = await asyncio.to_thread(
        run_script, text, None, timeout_ms=timeout_ms, extra_namespaces=sdk,
    )
    duration_ms = int((time.monotonic() - t0) * 1000)

    output = result.output or ""
    if result.variables:
        try:
            dump = json.dumps(result.variables, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            dump = str(result.variables)
        output = (output + "\n" if output else "") + "vars: " + dump

    truncated = len(output) > OUTPUT_EXCERPT_CHARS
    return {
        "ok": bool(result.ok),
        "output": output[:OUTPUT_EXCERPT_CHARS],
        "error": result.error,
        "duration_ms": duration_ms,
        "db_queries": state.db_queries,
        "http_calls": state.http_calls,
        "truncated": truncated,
        "title": (title or "").strip() or None,
    }


async def resolve_script_user_id(
    db, *, batch_id: Optional[int] = None, run_id: Optional[int] = None,
) -> Optional[int]:
    """执行链脚本身份：批次触发用户（RunBatch.triggered_by = username）→ user id。

    - ``batch_id`` 直接给出，或经 ``run_id``（agent_runs.goal->>'batch_id'）反查；
    - 解析失败返回 None（platform.* 将按最小权限拒绝；db.query 只读查询不受影响）。
    """
    if db is None:
        return None
    try:
        from app.crud.user import get_user_by_username
        from sqlalchemy import select

        from app.db_models import RunBatch

        bid = batch_id
        if bid is None and run_id is not None:
            from sqlalchemy import text as _text

            row = (
                await db.execute(
                    _text("SELECT (goal->>'batch_id')::int FROM agent_runs WHERE id=:r"),
                    {"r": int(run_id)},
                )
            ).first()
            bid = row[0] if row and row[0] else None
        if not bid:
            return None
        name = (
            await db.execute(select(RunBatch.triggered_by).where(RunBatch.id == int(bid)))
        ).scalar_one_or_none()
        username = str(name or "").strip()
        if not username:
            return None
        user = await get_user_by_username(db, username)
        return int(user.id) if user is not None else None
    except Exception:
        logger.debug("脚本身份解析失败 batch=%s run=%s", batch_id, run_id, exc_info=True)
        return None


__all__ = ["execute_script", "resolve_script_user_id", "OUTPUT_EXCERPT_CHARS"]
