# app/routers/quality/defect.py
# 041 US4：缺陷报告草稿 + 证据包下载。
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.config import get_settings
from app.database import get_async_db
from app.routers.quality.common import ensure_quality_enabled, get_owned_analysis, serialize
from app.services.quality_defect import REPORTS_ROOT, create_defect_report
from core.evidence_bundle import build_evidence_zip, collect_evidence_files

router = APIRouter()


class DefectReportRequest(BaseModel):
    project_id: int
    run_id: int | None = None
    group_signature: str | None = None
    days: int = 14
    force: bool = False


@router.post("/defect-reports")
async def create_defect_endpoint(
    payload: DefectReportRequest,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> dict:
    await ensure_quality_enabled(db, user, payload.project_id)
    if payload.run_id is None and not payload.group_signature:
        raise HTTPException(status_code=400, detail="需要 run_id 或 group_signature")
    try:
        row, cached = await create_defect_report(
            db, project_id=payload.project_id, run_id=payload.run_id,
            group_signature=payload.group_signature, days=payload.days,
            user_id=getattr(user, "id", None), force=payload.force,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    data = serialize(row)
    data["cached"] = cached
    return data


@router.get("/defect-reports/{analysis_id}/evidence.zip")
async def download_evidence_zip(
    analysis_id: int,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
):
    row = await get_owned_analysis(db, analysis_id=analysis_id, user=user)
    if row.kind != "defect":
        raise HTTPException(status_code=404, detail="缺陷分析不存在")
    await ensure_quality_enabled(db, user, row.project_id)
    files = (row.result or {}).get("evidence_files") or []
    settings = get_settings()
    dest = REPORTS_ROOT / f"quality_defect_{row.id}.zip"
    max_bytes = int(settings.quality_evidence_max_mb) * 1024 * 1024
    selected = collect_evidence_files(files, root=REPORTS_ROOT)
    if not selected:
        raise HTTPException(status_code=404, detail="没有可下载的证据文件")
    total = sum(f.stat().st_size for f in selected if f.exists())
    if total > max_bytes:
        raise HTTPException(status_code=413, detail="证据包超过大小上限")
    try:
        path, _names = build_evidence_zip(
            files, dest=dest, root=REPORTS_ROOT, max_bytes=max_bytes,
        )
    except ValueError as exc:
        msg = str(exc)
        status = 413 if "超过" in msg else 404
        raise HTTPException(status_code=status, detail=msg) from exc
    return FileResponse(path, media_type="application/zip", filename=f"defect_{row.id}.zip")
