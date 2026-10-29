# app/routers/quality/common.py
# 041 AI 质量智能：路由共享的权限/开关/序列化工具。
from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app import db_models
from app.auth import get_user_project_filter
from app.config import get_settings
from app.crud import api_ai as crud_api_ai


def ensure_project_access(user, project_id: int) -> None:
    allowed = get_user_project_filter(user)
    if allowed is not None and project_id not in allowed:
        raise HTTPException(status_code=403, detail="无权访问该项目")


async def ensure_quality_enabled(db: AsyncSession, user, project_id: int) -> None:
    """全局开关 + 项目级开关（quality_ai_enabled 优先，回退 api_ai_enabled）。"""
    settings = get_settings()
    if not getattr(settings, "quality_ai_enabled", True):
        raise HTTPException(status_code=403, detail="质量 AI 已关闭")
    ensure_project_access(user, project_id)
    project = await db.get(db_models.Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="项目不存在")
    ai_settings = project.ai_settings if isinstance(project.ai_settings, dict) else {}
    if ai_settings.get("quality_ai_enabled") is False:
        raise HTTPException(status_code=403, detail="该项目已关闭质量 AI")
    if "quality_ai_enabled" not in ai_settings and ai_settings.get("api_ai_enabled") is False:
        raise HTTPException(status_code=403, detail="该项目已关闭 AI 能力")


async def ensure_capability(db: AsyncSession, user, project_id: int, capability: str) -> None:
    """042 质量决策能力开关：全局 + 项目级。"""
    settings = get_settings()
    flag = {
        "impact": "quality_impact_enabled",
        "flaky": "quality_flaky_enabled",
        "traceability": "quality_traceability_enabled",
    }.get(capability)
    if flag and not getattr(settings, flag, True):
        raise HTTPException(status_code=403, detail=f"{capability} 能力已关闭")
    ensure_project_access(user, project_id)
    project = await db.get(db_models.Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="项目不存在")
    ai_settings = project.ai_settings if isinstance(project.ai_settings, dict) else {}
    if flag and ai_settings.get(flag) is False:
        raise HTTPException(status_code=403, detail=f"该项目已关闭 {capability} 能力")


async def project_id_for_run(db: AsyncSession, run: db_models.TestRun) -> int | None:
    if run.case_id:
        case = await db.get(db_models.TestCase, run.case_id)
        if case is not None:
            return case.project_id
    if run.batch_id:
        batch = await db.get(db_models.RunBatch, run.batch_id)
        if batch is not None:
            return batch.project_id
    return None


async def get_owned_analysis(
    db: AsyncSession,
    *,
    analysis_id: int,
    user,
) -> db_models.ApiAiAnalysis:
    row = await db.get(db_models.ApiAiAnalysis, analysis_id)
    if row is None:
        raise HTTPException(status_code=404, detail="分析不存在")
    ensure_project_access(user, row.project_id)
    return row


def serialize(row: db_models.ApiAiAnalysis) -> dict:
    return crud_api_ai.serialize_analysis(row)
