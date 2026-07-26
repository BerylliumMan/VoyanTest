"""``POST /api/gen/upload`` — accept uploaded document(s), persist a session
record and start the (potentially long-running) AI analysis on a daemon
thread.  The in-memory session is also stored in :mod:`app.routers.gen.state`
so the preview/import endpoints can read it back without re-running analysis.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from datetime import datetime
from io import BytesIO
from typing import List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from ... import crud
from ...auth import get_current_user
from ...database import get_async_db
from app.gen.constants import ALLOWED_EXTENSIONS
from .state import _lock, _sessions

logger = logging.getLogger(__name__)

# 跟踪后台分析 task，防止被 GC 回收
_gen_tasks: set = set()

router = APIRouter()

# 魔术字节签名对照表
_MAGIC_SIGNATURES: dict[str, list[bytes]] = {
    ".png": [b"\x89PNG\r\n\x1a\n"],
    ".jpg": [b"\xff\xd8\xff"],
    ".jpeg": [b"\xff\xd8\xff"],
    ".pdf": [b"%PDF-"],
    ".docx": [b"PK\x03\x04"],
}

# .md 无固定魔术字节，跳过二进制检查


def _check_extension(filename: str) -> str:
    ext = os.path.splitext(filename)[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(400, f"不支持的文件类型：'{filename}' (ext='{ext}')，仅支持 .docx/.md/.png/.jpg/.jpeg/.pdf")
    return ext


def _check_magic_bytes(data: bytes, ext: str) -> None:
    """校验文件头魔术字节，阻止伪装扩展名的攻击文件。"""
    sigs = _MAGIC_SIGNATURES.get(ext, [])
    if not sigs:
        return  # .md 等无固定签名的类型跳过
    if not any(data.startswith(sig) for sig in sigs):
        raise HTTPException(400, f"文件内容与扩展名 '{ext}' 不匹配，疑似伪装文件")


@router.get("/agents")
async def list_generation_agents(
    db: AsyncSession = Depends(get_async_db),
    user=Depends(get_current_user),
) -> list[dict]:
    """List generation AgentDefinitions for the gen page picker (non-admin)."""
    from app.crud.agent_definition import list_agent_definitions

    agents = await list_agent_definitions(db, agent_type="generation")
    # Prefer enabled agents for the picker; fall back to all if none enabled
    enabled = [a for a in agents if a.is_active]
    source = enabled or agents
    return [
        {
            "id": a.id,
            "name": a.name,
            "description": a.description or "",
            "skills": a.skills or [],
            "is_active": bool(a.is_active),
        }
        for a in source
    ]


@router.post("/upload")
async def upload_and_analyze(
    files: List[UploadFile] = File(...),
    project_description: str = Form(""),
    project_id: Optional[int] = Form(None),
    agent_id: Optional[int] = Form(None),
    db: AsyncSession = Depends(get_async_db),
    user=Depends(get_current_user),
) -> dict:
    """Upload document(s) and start AI analysis to generate test cases."""
    if not files:
        raise HTTPException(400, "请上传至少一个文件")

    # Resolve generation agent early (validate before starting background work)
    from app.crud.agent_definition import get_active_by_type, get_agent_definition
    from app.gen.prompts import pick_tc_prompt_key

    selected_agent = None
    if agent_id is not None:
        selected_agent = await get_agent_definition(db, agent_id)
        if selected_agent is None or selected_agent.agent_type != "generation":
            raise HTTPException(400, f"无效的生成 Agent id={agent_id}")
    else:
        selected_agent = await get_active_by_type(db, "generation")

    resolved_agent_id = selected_agent.id if selected_agent else None
    resolved_skills = list(selected_agent.skills or []) if selected_agent else []
    tc_prompt_key = pick_tc_prompt_key(resolved_skills)

    filenames = [f.filename or f"file_{i}" for i, f in enumerate(files)]

    file_contents = []
    for f in files:
        ext = _check_extension(f.filename or "")
        content = await f.read()
        _check_magic_bytes(content, ext)
        file_contents.append(BytesIO(content))

    from app.gen.models import AnalysisSession
    session_id = str(uuid.uuid4())
    session = AnalysisSession(
        session_id=session_id,
        filename=filenames[0] if filenames else "unknown",
        filenames=filenames,
        project_description=project_description,
        status="analyzing",
    )

    async with _lock:
        _sessions[session_id] = session

    await crud.create_gen_session(
        db,
        session_id=session_id,
        filename=filenames[0] if filenames else "unknown",
        filenames=json.dumps(filenames),
        project_id=project_id,
        project_description=project_description,
        user_id=user.id,
    )

    _last_db_progress = {"t": 0.0, "p": -1}

    async def _set_progress(percent: int, message: str) -> None:
        percent = max(0, min(100, int(percent)))
        async with _lock:
            session.progress = percent
            session.progress_message = message
        # 节流写入 DB，便于多 worker / 重启后 status 仍可读
        now = time.monotonic()
        if (
            abs(percent - _last_db_progress["p"]) >= 5
            or now - _last_db_progress["t"] >= 2.0
            or percent >= 100
        ):
            _last_db_progress["t"] = now
            _last_db_progress["p"] = percent
            try:
                from app.database import AsyncSessionLocal
                async with AsyncSessionLocal() as _pdb:
                    await crud.update_gen_session_progress(
                        _pdb, session_id, percent, message or "",
                    )
            except Exception:
                logger.debug("persist gen progress failed", exc_info=True)

    def _progress_callback(current: int, total: int, message: str) -> None:
        """Sync callback used by gen pipeline; schedule async session update."""
        if total and total > 0:
            percent = int(current / total * 100)
        else:
            # Heuristic by message keywords for UI stepper
            if "用例" in (message or "") or "校验" in (message or ""):
                percent = 75
            elif "功能点" in (message or "") or "提取" in (message or ""):
                percent = 45
            elif "解析" in (message or ""):
                percent = 15
            else:
                percent = max(session.progress, 10)
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(_set_progress(percent, message or ""))
        except RuntimeError:
            session.progress = percent
            session.progress_message = message or ""

    async def _run_full_analysis() -> None:
        # 等待原 API session 的 cleanup(_close_impl) 完成，避免连接池冲突
        await asyncio.sleep(0.1)
        try:
            from app.gen.analyzer import extract_multi_file_content, two_phase_analyze
            from app.database import AsyncSessionLocal

            await _set_progress(5, "正在解析文档")
            combined_text, _, warnings = await extract_multi_file_content(
                file_contents, filenames, progress_callback=_progress_callback,
            )

            if not combined_text.strip():
                async with _lock:
                    session.status = "failed"
                    session.error_message = "; ".join(warnings or ["未能从文件中提取到有效文本"])
                    session.progress_message = session.error_message
                await _update_db(session_id, "failed", session.error_message, 0, 0)
                return

            fp_prompt = None
            tc_prompt = None
            agent_name = selected_agent.name if selected_agent else None
            async with AsyncSessionLocal() as pdb:
                from app.runtime_config import resolve_prompt_for_agent
                try:
                    if selected_agent:
                        logger.info(
                            "Generation via AgentDefinition id=%s name=%s skills=%s tc_key=%s",
                            selected_agent.id, selected_agent.name,
                            selected_agent.skills, tc_prompt_key,
                        )
                    else:
                        logger.warning(
                            "No generation AgentDefinition; using PromptTemplate defaults"
                        )
                    fp_prompt = await resolve_prompt_for_agent(
                        pdb, "generation", "fp_extract", agent_id=resolved_agent_id,
                    )
                    tc_prompt = await resolve_prompt_for_agent(
                        pdb, "generation", tc_prompt_key, agent_id=resolved_agent_id,
                    )
                except Exception:
                    logger.debug("resolving prompts from DB failed, using defaults")

            await _set_progress(30, "正在提取功能点")
            result = await two_phase_analyze(
                combined_text,
                progress_callback=_progress_callback,
                project_description=project_description,
                db=None,
                prompts={"fp_extract": fp_prompt, "tc_generate": tc_prompt},
                agent_id=resolved_agent_id,
                skills=resolved_skills,
                tc_prompt_key=tc_prompt_key,
            )
            if agent_name and not result.get("error"):
                warnings_out = list(result.get("warnings") or [])
                mode = "UI自动化" if tc_prompt_key == "tc_generate_ui" else "功能用例"
                warnings_out.insert(0, f"使用 AI Agent：{agent_name}（{mode}）")
                result["warnings"] = warnings_out

            async with _lock:
                if result.get("error"):
                    session.status = "failed"
                    error_parts = list(result.get("warnings", [])) + list(warnings)
                    session.error_message = "; ".join(error_parts or ["分析失败"])
                    session.progress_message = session.error_message
                    session.progress = 100
                else:
                    session.functional_points = result["functional_points"]
                    session.test_cases = result["test_cases"]
                    session.status = "completed"
                    combined_warnings = (result.get("warnings") or []) + warnings
                    session.error_message = "; ".join(combined_warnings)
                    session.progress = 100
                    session.progress_message = "分析完成"

            if result.get("error"):
                await _update_db(session_id, "failed", session.error_message, 0, 0)
            else:
                await _update_db(
                    session_id, "completed", session.error_message,
                    len(result["functional_points"]), len(result["test_cases"]),
                    functional_points=result["functional_points"],
                    test_cases=result["test_cases"],
                )
        except Exception as e:
            logger.exception("Analysis failed")
            async with _lock:
                session.status = "failed"
                session.error_message = str(e)
                session.progress = 100
                session.progress_message = str(e)
            await _update_db(session_id, "failed", str(e), 0, 0)

    task = asyncio.create_task(_run_full_analysis())
    _gen_tasks.add(task)
    task.add_done_callback(_gen_tasks.discard)

    return {
        "session_id": session_id,
        "status": "analyzing",
        "agent_id": resolved_agent_id,
        "tc_prompt_key": tc_prompt_key,
    }


async def _update_db(session_id: str, status: str, error_msg: str, fp_count: int, tc_count: int,
                     functional_points: list = None, test_cases: list = None) -> None:
    """异步更新 GenSession DB 记录。"""
    from app.database import AsyncSessionLocal
    async with AsyncSessionLocal() as db:
        await crud.persist_gen_session_results(
            db, session_id,
            status=status,
            error_message=error_msg,
            functional_points_count=fp_count,
            test_cases_count=tc_count,
            completed_at=datetime.now() if status in ("completed", "failed") else None,
            functional_points=functional_points,
            test_cases=test_cases,
        )
