"""API router for CDP-based user action recording.

Endpoints (implemented inline; replaces the Wave-2 sub-router placeholders):

* ``POST /api/recordings/start``                  — start a new recording session
* ``POST /api/recordings/{session_id}/stop``      — stop an active recording
* ``GET  /api/recordings/{session_id}/events``    — read recorded events (non-destructive)
* ``POST /api/recordings/{session_id}/convert``   — convert recorded events to test steps via LLM

Shared in-memory state lives in :mod:`.state`; the Pydantic request/response
models live in :mod:`.schemas`.
"""
from __future__ import annotations

import logging
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException

from app.auth import get_current_user
from core.cdp_session import CDPRecordingSession
from core.cdp_converter import convert_events_to_steps
from core.browser_pool import BrowserPool

from .schemas import (
    StartRecordingRequest,
    RecordingStatusResponse,
    RecordedEventResponse,
    ConvertRequest,
    ConvertStepItem,
    ConvertResponse,
)
from .state import (
    get_session,
    get_session_for_user,
    create_session,
    stop_session as state_stop_session,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/recordings", tags=["录制回放"])


async def _pick_active_manager():
    """Async accessor that holds ``BrowserPool._lock`` while picking a manager.

    Iterating ``BrowserPool._instances`` while holding the lock ensures that
    another coroutine can't mutate the dict mid-iteration.
    """
    async with BrowserPool._lock:
        for _project_id, mgr in BrowserPool._instances.items():
            return mgr
    return None


@router.post("/start", response_model=RecordingStatusResponse)
async def start_recording(
    req: StartRecordingRequest,
    user=Depends(get_current_user),
) -> RecordingStatusResponse:
    """Start a new CDP recording session.

    Re-uses any active ``PlaywrightMCPManager`` from :class:`BrowserPool`,
    attaches the orchestrator, and (optionally) navigates to ``req.url``.
    """
    # 1) 一个用户同时只能有一个 active 录制会话。
    existing = await get_session_for_user(user.id)
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail=f"用户已有进行中的录制会话: {existing.session_id}",
        )

    # 2) 分配 session_id 并构造 orchestrator。
    session_id = f"rec-{uuid.uuid4().hex[:12]}"
    cdp_rec_session = CDPRecordingSession(session_id)

    # 3) 从 BrowserPool 取一个活跃的 manager；都没有就 503。
    manager = await _pick_active_manager()
    if manager is None:
        raise HTTPException(
            status_code=503,
            detail="无可用的浏览器实例，请先在某个项目中启动浏览器后再开始录制",
        )

    # 4) 启动 CDP 录制（attaches via PlaywrightMCPManager.call_tool）。
    started = await cdp_rec_session.start_recording(manager)
    if not started:
        raise HTTPException(status_code=500, detail="CDP 录制启动失败")

    # 5) 可选：导航到目标 URL。失败不阻塞会话——用户可以稍后手动导航。
    if req.url:
        navigate = getattr(manager, "call_tool", None)
        if navigate is not None:
            try:
                await navigate("browser_navigate", {"url": req.url})
            except Exception as exc:
                logger.warning(
                    "录制会话 %s 导航到 %s 失败: %s", session_id, req.url, exc
                )

    # 6) 把会话登记到 state store。
    await create_session(
        session_id=session_id,
        user_id=user.id,
        url=req.url,
        page_title=req.page_title,
        cdp_session_ref=cdp_rec_session,
    )

    logger.info(
        "录制会话已启动: session_id=%s user_id=%s url=%s",
        session_id,
        getattr(user, "id", None),
        req.url,
    )

    return RecordingStatusResponse(
        session_id=session_id,
        status="recording",
        url=req.url,
        page_title=req.page_title,
        elapsed_seconds=0.0,
        events_count=0,
    )


@router.post("/{session_id}/stop", response_model=RecordingStatusResponse)
async def stop_recording(
    session_id: str,
    user=Depends(get_current_user),
) -> RecordingStatusResponse:
    """Stop an active recording session and detach the CDP transport."""
    state = await get_session(session_id)
    if state is None:
        raise HTTPException(
            status_code=404, detail=f"录制会话不存在: {session_id}"
        )

    if state.status != "recording":
        raise HTTPException(
            status_code=400,
            detail=f"录制会话状态非 recording (当前: {state.status})，无法停止",
        )

    cdp_session = state.cdp_session_ref
    stop_fn = getattr(cdp_session, "stop_recording", None) if cdp_session is not None else None
    if stop_fn is not None:
        try:
            await stop_fn()
        except Exception as exc:
            logger.warning(
                "停止 CDP 录制失败 (session_id=%s): %s", session_id, exc
            )

    await state_stop_session(session_id)

    elapsed = 0.0
    events_count = 0
    if cdp_session is not None:
        elapsed = float(getattr(cdp_session, "elapsed_seconds", 0.0) or 0.0)
        events_count = int(getattr(cdp_session, "events_count", 0) or 0)

    logger.info(
        "录制会话已停止: session_id=%s elapsed=%.2fs events=%d",
        session_id,
        elapsed,
        events_count,
    )

    return RecordingStatusResponse(
        session_id=session_id,
        status="stopped",
        url=state.url,
        page_title=state.page_title,
        elapsed_seconds=elapsed,
        events_count=events_count,
    )


@router.get("/{session_id}/events", response_model=list[RecordedEventResponse])
async def get_recorded_events(
    session_id: str,
    user=Depends(get_current_user),
) -> list[RecordedEventResponse]:
    """Return the recorded events **without** clearing the in-memory buffer.

    The endpoint reads via ``CDPRecordingSession.get_events()`` when the
    orchestrator exposes one; otherwise it returns an empty list rather than
    consuming the buffer (so :func:`convert` can still see the events later).
    """
    state = await get_session(session_id)
    if state is None:
        raise HTTPException(
            status_code=404, detail=f"录制会话不存在: {session_id}"
        )

    cdp_session = state.cdp_session_ref
    get_events = getattr(cdp_session, "get_events", None) if cdp_session else None
    if get_events is None:
        return []

    try:
        raw_events = get_events()
    except Exception as exc:
        logger.warning(
            "读取录制事件失败 (session_id=%s): %s", session_id, exc
        )
        return []

    return [RecordedEventResponse(**e.to_dict()) for e in raw_events]


@router.post("/{session_id}/convert", response_model=ConvertResponse)
async def convert_to_test_steps(
    req: ConvertRequest,
    session_id: str,
    user=Depends(get_current_user),
) -> ConvertResponse:
    """Collect the recorded events and convert them into test steps via LLM.

    The events buffer is **drained** here (``collect_events`` semantics), so
    calling :func:`convert` more than once on the same session will see an
    empty event list after the first call.
    """
    # 请求体里也带 session_id；优先用 path 参数的，校验二者一致。
    if req.session_id and req.session_id != session_id:
        raise HTTPException(
            status_code=400,
            detail=(
                f"路径 session_id 与 body session_id 不一致: "
                f"{session_id} != {req.session_id}"
            ),
        )

    state = await get_session(session_id)
    if state is None:
        raise HTTPException(
            status_code=404, detail=f"录制会话不存在: {session_id}"
        )

    cdp_session = state.cdp_session_ref
    collect_events = getattr(cdp_session, "collect_events", None) if cdp_session else None
    if collect_events is None:
        events = []
    else:
        try:
            events = collect_events()
        except Exception as exc:
            logger.warning(
                "collect_events 失败 (session_id=%s): %s", session_id, exc
            )
            events = []

    event_dicts = [e.to_dict() for e in events]

    if not event_dicts:
        return ConvertResponse(
            session_id=session_id,
            page_title=state.page_title,
            steps=[],
            events_count=0,
        )
    try:
        raw_steps = await convert_events_to_steps(
            events=event_dicts,
            page_title=state.page_title or "",
        )
    except Exception as exc:
        logger.error(
            "CDP 事件 → 测试步骤 转换失败 (session_id=%s): %s",
            session_id,
            exc,
            exc_info=True,
        )
        raise HTTPException(
            status_code=500, detail=f"LLM 转换失败: {exc}"
        )

    steps = [
        ConvertStepItem(
            step_description=str(s.get("step_description", "") or "").strip(),
            expected_result=str(s.get("expected_result", "") or "").strip(),
        )
        for s in (raw_steps or [])
    ]

    logger.info(
        "录制转换完成: session_id=%s events=%d steps=%d",
        session_id,
        len(event_dicts),
        len(steps),
    )

    return ConvertResponse(
        session_id=session_id,
        page_title=state.page_title,
        steps=steps,
        events_count=len(event_dicts),
    )


__all__ = ["router"]
