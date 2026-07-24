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
import uuid
from datetime import datetime
from io import BytesIO
from typing import List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from ... import db_models
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


@router.post("/upload")
async def upload_and_analyze(
    files: List[UploadFile] = File(...),
    project_description: str = Form(""),
    project_id: Optional[int] = Form(None),
    db: AsyncSession = Depends(get_async_db),
    user=Depends(get_current_user),
) -> dict:
    """Upload document(s) and start AI analysis to generate test cases."""
    if not files:
        raise HTTPException(400, "请上传至少一个文件")

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

    async def _run_full_analysis() -> None:
        # 等待原 API session 的 cleanup(_close_impl) 完成，避免连接池冲突
        await asyncio.sleep(0.1)
        try:
            from app.gen.analyzer import extract_multi_file_content, two_phase_analyze
            from app.database import AsyncSessionLocal

            combined_text, _, warnings = await extract_multi_file_content(
                file_contents, filenames
            )

            # 提前解析提示词（需要 DB），然后关闭 session，避免 AI 分析期间 LLM 调用
            # 新建 session 时发生连接池冲突（_connection_for_bind vs close）
            fp_prompt = None
            tc_prompt = None
            async with AsyncSessionLocal() as pdb:
                from app.runtime_config import resolve_prompt_for_agent
                try:
                    fp_prompt = await resolve_prompt_for_agent(pdb, "generation", "fp_extract")
                    tc_prompt = await resolve_prompt_for_agent(pdb, "generation", "tc_generate")
                except Exception:
                    logger.debug("resolving prompts from DB failed, using defaults")
            # pdb 已关闭，AI 分析期间无 DB 连接
            result = await two_phase_analyze(
                combined_text,
                project_description=project_description,
                db=None,
                prompts={"fp_extract": fp_prompt, "tc_generate": tc_prompt},
            )

            async with _lock:
                if result.get("error"):
                    session.status = "failed"
                    error_parts = list(result.get("warnings", [])) + list(warnings)
                    session.error_message = "; ".join(error_parts or ["分析失败"])
                else:
                    session.functional_points = result["functional_points"]
                    session.test_cases = result["test_cases"]
                    session.status = "completed"
                    combined_warnings = (result.get("warnings") or []) + warnings
                    session.error_message = "; ".join(combined_warnings)

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
            await _update_db(session_id, "failed", str(e), 0, 0)

    # 用 async task 替代 threading.Thread，避免 event loop 冲突
    task = asyncio.create_task(_run_full_analysis())
    _gen_tasks.add(task)
    task.add_done_callback(_gen_tasks.discard)

    return {"session_id": session_id, "status": "analyzing"}


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
