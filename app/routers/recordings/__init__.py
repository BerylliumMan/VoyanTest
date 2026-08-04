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
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import crud, db_models
from app.auth import get_current_user, get_user_project_filter
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
    touch_session_activity,
    finalize_recording_session,
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
        elapsed_seconds=time.time() - (state.start_time or time.time()),
        events_count=(
            int(getattr(state.cdp_session_ref, "events_count", 0) or 0)
            if state.cdp_session_ref is not None
            else (state.events_count or 0)
        ),
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
    # 1) 一个用户同时只能有一个 active 录制会话。如果有残留的脏会话，自动清理。
    existing = await get_session_for_user(user.id)
    if existing is not None:
        # 检查是否真的是活跃会话（有 CDP session 引用且状态为 recording）
        cdp_ref = getattr(existing, 'cdp_session_ref', None)
        if cdp_ref is not None and existing.status == 'recording':
            raise HTTPException(
                status_code=409,
                detail=f"用户已有进行中的录制会话: {existing.session_id}",
            )
        # 否则是脏会话，自动清理
        from .state import remove_session
        await remove_session(existing.session_id)

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
    if state.user_id != user.id:
        raise HTTPException(status_code=404, detail="Recording session not found")

    if state.status != "recording":
        raise HTTPException(
            status_code=400,
            detail=f"录制会话状态非 recording (当前: {state.status})，无法停止",
        )

    await finalize_recording_session(state, reason="user_stop")
    await state_stop_session(session_id)

    cdp_session = state.cdp_session_ref
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
    allowed_ids = get_user_project_filter(user)
    stmt = (
        select(db_models.RecordingSession)
        .order_by(db_models.RecordingSession.started_at.desc())
        .limit(50)
    )
    if allowed_ids is not None:
        stmt = stmt.where(db_models.RecordingSession.user_id == user.id)
    result = await db.execute(stmt)
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
    if rec.user_id != user.id:
        raise HTTPException(status_code=404, detail="Recording not found")
    await db.delete(rec)
    await db.commit()
    return {"deleted": True}


@router.get("/{session_id}/events", response_model=list[RecordedEventResponse])
async def get_recorded_events(
    session_id: str,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> list[RecordedEventResponse]:
    """Return the recorded events **without** clearing the in-memory buffer.

    The endpoint reads via ``CDPRecordingSession.get_events()`` when the
    orchestrator exposes one; otherwise it reads from DB (historical).
    """
    state = await get_session(session_id)
    if state is None:
        # 历史录制：从 DB 读
        _rec = (await db.execute(
            select(db_models.RecordingSession).where(
                db_models.RecordingSession.session_id == session_id
            )
        )).scalar_one_or_none()
        if _rec and _rec.user_id != user.id:
            raise HTTPException(status_code=404, detail="Recording not found")
        if _rec is None or not _rec.events_data:
            raise HTTPException(
                status_code=404, detail=f"录制会话不存在或无可录制事件: {session_id}"
            )
        import json
        return [RecordedEventResponse(**e) for e in json.loads(_rec.events_data)]

    if state.user_id != user.id:
        raise HTTPException(status_code=404, detail="Session not found")

    cdp_session = state.cdp_session_ref
    get_events = getattr(cdp_session, "get_events", None) if cdp_session else None
    if get_events is None:
        return []

    try:
        raw_events = get_events()
    except (RuntimeError, ConnectionError, AttributeError) as exc:
        logger.warning(
            "读取录制事件失败 (session_id=%s): %s", session_id, exc
        )
        return []

    await touch_session_activity(session_id, events_count=len(raw_events))
    return [RecordedEventResponse(**e.to_dict()) for e in raw_events]


async def _load_recording_event_dicts(session_id: str, state, db: AsyncSession | None = None) -> list[dict]:
    """非破坏性读取录制事件：优先内存 get_events，其次 DB events_data。"""
    import json

    cdp_session = getattr(state, "cdp_session_ref", None)
    get_events = getattr(cdp_session, "get_events", None) if cdp_session else None
    if get_events is not None:
        try:
            events = get_events()
            if events:
                return [e.to_dict() if hasattr(e, "to_dict") else e for e in events]
        except (RuntimeError, ConnectionError, AttributeError) as exc:
            logger.warning("get_events 失败 (session_id=%s): %s", session_id, exc)

    async def _from_db(session: AsyncSession) -> list[dict]:
        result = await session.execute(
            select(db_models.RecordingSession).where(
                db_models.RecordingSession.session_id == session_id
            )
        )
        row = result.scalar_one_or_none()
        if row and row.events_data:
            data = json.loads(row.events_data) if isinstance(row.events_data, str) else row.events_data
            if isinstance(data, list) and data:
                return data
        return []

    try:
        if db is not None:
            return await _from_db(db)
        async with AsyncSessionLocal() as session:
            return await _from_db(session)
    except Exception:
        logger.warning("从 DB 读取录制事件失败 session_id=%s", session_id, exc_info=True)
    return []


@router.post("/{session_id}/convert", response_model=ConvertResponse)
async def convert_to_test_steps(
    req: ConvertRequest,
    session_id: str,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> ConvertResponse:
    """读取录制事件并转换为测试步骤（不 drain 内存缓冲，可重复转换）。

    每次请求都重新从当前事件跑规则/LLM 转换，不读取历史 convert 结果。
    ``force=true``（默认）显式声明强制重新生成，便于前端二次点击语义对齐。
    """
    if req.session_id and req.session_id != session_id:
        raise HTTPException(
            status_code=400,
            detail=(
                f"路径 session_id 与 body session_id 不一致: "
                f"{session_id} != {req.session_id}"
            ),
        )

    force = bool(getattr(req, "force", True))
    state = await get_session(session_id)
    page_title = ""
    if state is not None:
        if state.user_id != user.id:
            raise HTTPException(status_code=404, detail="Session not found")
        page_title = state.page_title or ""
        event_dicts = await _load_recording_event_dicts(session_id, state, db)
    else:
        # 历史会话：仅内存已清理时从 DB 读取
        result = await db.execute(
            select(db_models.RecordingSession).where(
                db_models.RecordingSession.session_id == session_id
            )
        )
        row = result.scalar_one_or_none()
        if row is None or (row.user_id is not None and row.user_id != user.id):
            raise HTTPException(status_code=404, detail=f"录制会话不存在: {session_id}")
        page_title = row.page_title or ""
        event_dicts = await _load_recording_event_dicts(session_id, type("S", (), {"cdp_session_ref": None})(), db)

    logger.info(
        "录制转换开始: session_id=%s force=%s events=%d",
        session_id,
        force,
        len(event_dicts or []),
    )

    if not event_dicts:
        return ConvertResponse(
            session_id=session_id,
            page_title=page_title,
            steps=[],
            events_count=0,
        )
    try:
        # 始终用当前事件重新转换；force 仅作显式语义（无结果缓存可跳过）
        raw_steps = await convert_events_to_steps(
            events=event_dicts,
            page_title=page_title or "",
        )
    except (openai.OpenAIError, asyncio.TimeoutError, OSError, ValueError) as exc:
        logger.exception(
            "CDP 事件 → 测试步骤 转换失败 (session_id=%s)", session_id,
        )
        raise HTTPException(
            status_code=500, detail=f"LLM 转换失败: {exc}"
        )

    steps = []
    for s in (raw_steps or []):
        structured = s.get("structured_step") if isinstance(s.get("structured_step"), dict) else None
        if structured is None and s.get("action"):
            structured = {
                k: s.get(k)
                for k in (
                    "action", "target_name", "target_role", "value",
                    "disambiguation", "icon_hint", "frame_hint", "note",
                    "selector",
                )
                if s.get(k) not in (None, "")
            }
        steps.append(
            ConvertStepItem(
                step_description=str(s.get("step_description", "") or "").strip(),
                expected_result=str(s.get("expected_result", "") or "").strip(),
                action=s.get("action") or (structured or {}).get("action"),
                target_name=s.get("target_name") or (structured or {}).get("target_name"),
                target_role=s.get("target_role") or (structured or {}).get("target_role"),
                value=s.get("value") if s.get("value") is not None else (structured or {}).get("value"),
                selector=s.get("selector") or (structured or {}).get("selector"),
                structured_step=structured,
            )
        )

    logger.info(
        "录制转换完成: session_id=%s events=%d steps=%d",
        session_id,
        len(event_dicts),
        len(steps),
    )

    return ConvertResponse(
        session_id=session_id,
        page_title=page_title,
        steps=steps,
        events_count=len(event_dicts),
    )


@router.post("/{session_id}/replay")
async def replay_recording(
    session_id: str,
    background_tasks: BackgroundTasks,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> dict:
    """把录制转为临时用例并真正提交执行。"""
    state = await get_session(session_id)
    if state is not None and state.user_id != user.id:
        raise HTTPException(status_code=404, detail="Session not found")

    if state is not None:
        event_dicts = await _load_recording_event_dicts(session_id, state, db)
        page_title = state.page_title or ""
    else:
        result = await db.execute(
            select(db_models.RecordingSession).where(
                db_models.RecordingSession.session_id == session_id
            )
        )
        row = result.scalar_one_or_none()
        if row is None or (row.user_id is not None and row.user_id != user.id):
            raise HTTPException(status_code=404, detail="录制会话不存在")
        page_title = row.page_title or ""
        event_dicts = await _load_recording_event_dicts(
            session_id, type("S", (), {"cdp_session_ref": None})(), db,
        )

    if not event_dicts:
        raise HTTPException(status_code=400, detail="没有录制事件")

    try:
        raw_steps = await convert_events_to_steps(
            events=event_dicts, page_title=page_title,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"LLM 转换失败: {exc}") from exc

    if not raw_steps:
        raise HTTPException(status_code=400, detail="转换后步骤为空")

    allowed_ids = get_user_project_filter(user)
    user_projects = await crud.list_projects_for_user(db, allowed_ids)
    if not user_projects:
        raise HTTPException(status_code=400, detail="当前用户没有可用的项目，无法回放录制")
    target_project = user_projects[0]

    tc = db_models.TestCase(
        project_id=target_project.id,
        module_id=None,
        project_case_number=await crud.get_next_project_case_number(db, target_project.id),
        name=f"录制回放-{session_id[:8]}-{datetime.utcnow().strftime('%H%M%S')}",
        description=f"由录制会话 {session_id} 自动生成",
        case_kind="ui",
    )
    db.add(tc)
    await db.flush()
    for i, s in enumerate(raw_steps):
        db.add(db_models.TestStep(
            case_id=tc.id,
            step_order=i + 1,
            description=str(s.get("step_description", "") or "").strip(),
            parsed_result=str(s.get("expected_result", "") or "").strip(),
        ))
    batch = await crud.create_run_batch(
        db, project_id=target_project.id, total_cases=1,
        triggered_by=getattr(user, "username", None),
    )
    await db.commit()
    await db.refresh(tc)

    case_id = tc.id
    batch_id = batch.id
    from app.routers.testcase import execution as _exec

    background_tasks.add_task(_exec.run_test_case, case_id, batch_id)

    return {
        "message": "录制回放已启动",
        "batch_id": batch_id,
        "case_id": case_id,
        "steps_count": len(raw_steps),
    }


__all__ = ["router"]


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

    allowed_ids = get_user_project_filter(user)
    if allowed_ids is not None and req.project_id not in allowed_ids:
        raise HTTPException(status_code=404, detail="Project not found")

    case_kind = (req.case_kind or "ui").strip().lower()
    if case_kind not in ("ui", "functional"):
        case_kind = "ui"

    from core.step_normalize import coerce_structured_step, parse_instant_to_structured
    from app.models import schemas as api_schemas

    step_payloads: list[api_schemas.TestStepCreatePayload] = []
    for i, step in enumerate(req.steps):
        structured = step.structured_step if isinstance(step.structured_step, dict) else None
        if structured is None and step.action:
            structured = {
                k: getattr(step, k)
                for k in (
                    "action", "target_name", "target_role", "value", "selector",
                )
                if getattr(step, k, None) not in (None, "")
            }
        if structured is None and case_kind == "ui" and step.step_description:
            structured = parse_instant_to_structured(step.step_description)
        if isinstance(structured, dict):
            structured = coerce_structured_step(structured) or structured
        desc = (step.step_description or "").strip() or f"步骤 {i + 1}"
        step_payloads.append(
            api_schemas.TestStepCreatePayload(
                step_order=i + 1,
                description=desc,
                parsed_result=step.expected_result,
                structured_step=structured if isinstance(structured, dict) else None,
            )
        )

    created = await crud.create_test_case(
        db,
        api_schemas.TestCaseCreate(
            project_id=req.project_id,
            module_id=req.module_id,
            name=req.name,
            case_kind=case_kind,
            steps=step_payloads,
        ),
    )

    return SaveAsCaseResponse(
        case_id=created.id, name=created.name, steps_count=len(req.steps),
    )
