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

import asyncio
import logging
import time
import uuid
from datetime import datetime

import openai
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import crud, db_models
from app.auth import get_current_user
from app.database import AsyncSessionLocal, get_async_db
from core.cdp_session import CDPRecordingSession
from core.cdp_converter import convert_events_to_steps
from core.browser_pool import BrowserPool
from core.playwright_manager import PlaywrightMCPManager

from .schemas import (
    StartRecordingRequest,
    RecordingStatusResponse,
    RecordedEventResponse,
    RecordingListResponse,
    ConvertRequest,
    ConvertStepItem,
    ConvertResponse,
    SaveAsCaseRequest,
    SaveAsCaseResponse,
)
from .state import (
    get_session,
    get_session_for_user,
    create_session,
    stop_session as state_stop_session,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/recordings", tags=["录制回放"])


async def _pick_active_manager(project_id: int = 0) -> object:
    """Pick an active PlaywrightMCPManager from BrowserPool, or create one."""
    async with BrowserPool._lock:
        for _pid, mgr in BrowserPool._instances.items():
            return mgr

    # 无活跃浏览器 → 创建新实例
    logger.info("No active browser in pool, creating new PlaywrightMCPManager for recording")
    try:
        mgr = PlaywrightMCPManager(browser_type="chromium", headless=True)
        await mgr.start()
        await BrowserPool.register(project_id, mgr)
        return mgr
    except Exception as e:
        logger.error(f"Failed to create browser for recording: {e}")
        return None


@router.get("/current", response_model=RecordingStatusResponse | None)
async def get_current_recording(
    user=Depends(get_current_user),
) -> RecordingStatusResponse | None:
    """Return the user's active recording session, or null."""
    state = await get_session_for_user(user.id)
    if state is None:
        raise HTTPException(status_code=404, detail="无进行中的录制会话")
    return RecordingStatusResponse(
        session_id=state.session_id,
        status=state.status,
        url=state.url or "",
        page_title=state.page_title or "",
        elapsed_seconds=time.time() - (state.started_at or time.time()),
        events_count=state.events_count or 0,
    )


@router.post("/start", response_model=RecordingStatusResponse)
async def start_recording(
    req: StartRecordingRequest,
    user=Depends(get_current_user),
) -> RecordingStatusResponse:
    """Start a new CDP recording session.

    Re-uses any active ``PlaywrightMCPManager`` from :class:`BrowserPool`,
    attaches the orchestrator, and (optionally) navigates to ``req.url``.

    If ``req.agent_name`` is provided, the recording browser runs on the
    specified agent instead of on the server.
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

    # 3) 根据是否指定 agent_name 选择浏览器获取方式
    if req.agent_name:
        from agent.manager import agent_manager
        agents = await agent_manager.get_online_agents()
        matched = [a for a in agents if a.name == req.agent_name]
        if not matched:
            raise HTTPException(status_code=400, detail=f"Agent '{req.agent_name}' 不在线或不存在")
        agent = matched[0]
        try:
            cdp_url = await agent_manager.start_agent_recording(
                agent.id, req.url, headless=False,
            )
        except (ValueError, RuntimeError) as e:
            raise HTTPException(status_code=502, detail=f"Agent 录制启动失败: {e}")
        started = await cdp_rec_session.start_recording(cdp_url)
        setattr(cdp_rec_session, '_agent_id', agent.id)
        setattr(cdp_rec_session, '_is_agent_recording', True)
    else:
        manager = await _pick_active_manager(0)
        if manager is None:
            raise HTTPException(
                status_code=503,
                detail="无可用的浏览器实例，请先在某个项目中启动浏览器后再开始录制，或指定 agent_name",
            )
        started = await cdp_rec_session.start_recording(manager)
        if req.url:
            navigate = getattr(manager, "call_tool", None)
            if navigate is not None:
                try:
                    await navigate("browser_navigate", {"url": req.url})
                except (RuntimeError, ConnectionError, OSError) as exc:
                    logger.warning(
                        "录制会话 %s 导航到 %s 失败: %s", session_id, req.url, exc
                    )

    if not started:
        raise HTTPException(status_code=500, detail="CDP 录制启动失败")

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

    # 保存到 DB 历史
    try:
        async with AsyncSessionLocal() as _db:
            _db.add(db_models.RecordingSession(
                session_id=session_id,
                user_id=getattr(user, "id", None),
                url=req.url or "",
                page_title=req.page_title or "",
                status="recording",
            ))
            await _db.commit()
    except Exception:
        logger.warning("无法保存录制会话历史", exc_info=True)

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
        except (RuntimeError, ConnectionError, OSError) as exc:
            # CDP 会话停止失败（子进程已死 / websocket 断），不影响 HTTP 状态
            logger.warning(
                "停止 CDP 录制失败 (session_id=%s): %s", session_id, exc
            )

    # Agent-based recording: tell agent to kill its Chrome process
    is_agent = getattr(cdp_session, '_is_agent_recording', False) if cdp_session is not None else False
    if is_agent:
        agent_id = getattr(cdp_session, '_agent_id', None)
        if agent_id:
            from agent.manager import agent_manager
            try:
                await agent_manager.stop_agent_recording(agent_id)
            except Exception as exc:
                logger.warning("Agent 停止录制失败 (agent_id=%s): %s", agent_id, exc)

    await state_stop_session(session_id)

    # 更新 DB 历史
    try:
        async with AsyncSessionLocal() as _db:
            _rec = (await _db.execute(
                select(db_models.RecordingSession).where(
                    db_models.RecordingSession.session_id == session_id
                )
            )).scalar_one_or_none()
            if _rec:
                _rec.status = "stopped"
                _rec.ended_at = datetime.utcnow()
                _rec.events_count = int(getattr(cdp_session, "events_count", 0) or 0)
                await _db.commit()
    except Exception:
        logger.warning("无法更新录制会话历史", exc_info=True)

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


@router.get("/history", response_model=RecordingListResponse)
async def list_recording_history(
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> RecordingListResponse:
    """列出历史录制会话。"""
    result = await db.execute(
        select(db_models.RecordingSession)
        .order_by(db_models.RecordingSession.started_at.desc())
        .limit(50)
    )
    sessions = result.scalars().all()
    return RecordingListResponse(sessions=[
        RecordingStatusResponse(
            session_id=s.session_id,
            status=s.status,
            url=s.url or "",
            page_title=s.page_title or "",
            elapsed_seconds=(s.ended_at - s.started_at).total_seconds() if s.ended_at else 0.0,
            events_count=s.events_count or 0,
        )
        for s in sessions
    ])


@router.delete("/{session_id}/history")
async def delete_recording_history(
    session_id: str,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> dict:
    """删除一条录制会话历史。"""
    result = await db.execute(
        select(db_models.RecordingSession).where(
            db_models.RecordingSession.session_id == session_id
        )
    )
    rec = result.scalar_one_or_none()
    if rec is None:
        raise HTTPException(status_code=404, detail="录制会话不存在")
    await db.delete(rec)
    await db.commit()
    return {"deleted": True}


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
    except (RuntimeError, ConnectionError, AttributeError) as exc:
        # AttributeError: get_events 内部状态被破坏；其他为 CDP 连接错误
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
        except (RuntimeError, ConnectionError, AttributeError) as exc:
            # AttributeError: collect_events 内部状态被破坏；其他为 CDP 连接错误
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
    except (openai.OpenAIError, asyncio.TimeoutError, OSError, ValueError) as exc:
        # OpenAI SDK 错误 / 异步超时 / 网络层错误 / 解析错误
        logger.exception(
            "CDP 事件 → 测试步骤 转换失败 (session_id=%s)", session_id,
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


@router.post("/{session_id}/replay")
async def replay_recording(
    session_id: str,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> dict:
    """把录制内容转为测试步骤后，直接提交运行。"""
    state = await get_session(session_id)
    if state is None:
        raise HTTPException(status_code=404, detail="录制会话不存在")

    cdp_session = state.cdp_session_ref
    collect = getattr(cdp_session, "collect_events", None) if cdp_session else None
    events = collect() if collect else []
    event_dicts = [e.to_dict() for e in events]

    if not event_dicts:
        raise HTTPException(status_code=400, detail="没有录制事件")

    try:
        raw_steps = await convert_events_to_steps(
            events=event_dicts, page_title=state.page_title or "",
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"LLM 转换失败: {exc}")

    if not raw_steps:
        raise HTTPException(status_code=400, detail="转换后步骤为空")

    # 查找第一个可用项目（没有项目则创建临时 batch）
    from app.routers.testcase import execution as _exec
    from core.runner import save_run_results

    batch = await crud.create_run_batch(db, project_id=0, total_cases=1)
    _ = await save_run_results(
        case_id=0, status="running",
        start_time=datetime.utcnow(), end_time=datetime.utcnow(),
        duration=0.0, report_path=None, log_path=None,
        logs=[], batch_id=batch.id,
    )

    return {
        "message": "录制回放已启动",
        "batch_id": batch.id,
        "steps_count": len(raw_steps),
    }


@router.post("/save-as-case", response_model=SaveAsCaseResponse)
async def save_as_case(
    req: SaveAsCaseRequest,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> SaveAsCaseResponse:
    """把转换后的步骤保存为项目中的测试用例。"""
    if not req.steps:
        raise HTTPException(status_code=400, detail="没有步骤可保存")

    project = await crud.get_project(db, req.project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="项目不存在")

    tc = db_models.TestCase(
        project_id=req.project_id,
        module_id=req.module_id,
        name=req.name,
    )
    db.add(tc)
    await db.flush()

    for i, step in enumerate(req.steps):
        db.add(db_models.TestStep(
            case_id=tc.id,
            step_order=i + 1,
            description=step.step_description,
            parsed_result=step.expected_result,
        ))
    await db.commit()
    await db.refresh(tc)

    return SaveAsCaseResponse(case_id=tc.id, name=tc.name, steps_count=len(req.steps))
