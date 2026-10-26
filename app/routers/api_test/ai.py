# app/routers/api_test/ai.py
# 040 接口测试 AI 端点（失败归因 MVP；其余能力后续扩展）
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app import db_models
from app.auth import get_current_user, get_user_project_filter
from app.config import get_settings
from app.crud import api_ai as crud_api_ai
from app.database import get_async_db
from app.services.api_ai import ApiAiUnavailable
from app.services.api_ai_analysis import analyze_failure

router = APIRouter()


def _ensure_project_access(user, project_id: int) -> None:
    allowed = get_user_project_filter(user)
    if allowed is not None and project_id not in allowed:
        raise HTTPException(status_code=403, detail="无权访问该项目")


async def _ensure_ai_enabled(db: AsyncSession, user, project_id: int, capability: str) -> None:
    settings = get_settings()
    if not settings.api_ai_enabled:
        raise HTTPException(status_code=403, detail="AI 能力已关闭")
    flag = getattr(settings, f"api_ai_{capability}_enabled", True)
    if not flag:
        raise HTTPException(status_code=403, detail=f"AI {capability} 能力已关闭")
    _ensure_project_access(user, project_id)
    project = await db.get(db_models.Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="项目不存在")
    ai_settings = project.ai_settings if isinstance(project.ai_settings, dict) else {}
    if ai_settings.get("api_ai_enabled") is False:
        raise HTTPException(status_code=403, detail="该项目已关闭 AI 能力")


async def _run_project_id(db: AsyncSession, run: db_models.TestRun) -> int | None:
    if run.case_id:
        case = await db.get(db_models.TestCase, run.case_id)
        if case is not None:
            return case.project_id
    if run.batch_id:
        batch = await db.get(db_models.RunBatch, run.batch_id)
        if batch is not None:
            return batch.project_id
    return None


class AnalyzeFailureRequest(BaseModel):
    run_id: int
    force: bool = False


class GenerateFromTextRequest(BaseModel):
    project_id: int
    text: str
    target: str = "case"  # case|scenario


class GenerateDataRequest(BaseModel):
    project_id: int
    definition_id: int
    categories: list[str] = []
    rows: int = 10
    save: bool = False
    name: str | None = None


class CheckContractRequest(BaseModel):
    run_id: int
    definition_id: int | None = None
    force: bool = False


class StrategyRequest(BaseModel):
    project_id: int
    force: bool = False
    progress_id: str | None = None


@router.post("/ai/analyze-failure")
async def analyze_failure_endpoint(
    payload: AnalyzeFailureRequest,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> dict:
    run = await db.get(db_models.TestRun, payload.run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="运行记录不存在")
    project_id = await _run_project_id(db, run)
    if project_id is None:
        raise HTTPException(status_code=404, detail="运行记录未关联项目")
    await _ensure_ai_enabled(db, user, project_id, "analysis")
    if run.status not in ("failed", "error"):
        raise HTTPException(status_code=400, detail="仅失败运行可做 AI 分析")
    try:
        row, cached = await analyze_failure(
            db, run=run, project_id=project_id,
            user_id=getattr(user, "id", None), force=payload.force,
        )
    except ApiAiUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    data = crud_api_ai.serialize_analysis(row)
    data["cached"] = cached
    return data


@router.post("/ai/generate-from-text")
async def generate_from_text_endpoint(
    payload: GenerateFromTextRequest,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> dict:
    await _ensure_ai_enabled(db, user, payload.project_id, "generation")
    if payload.target not in ("case", "scenario"):
        raise HTTPException(status_code=400, detail="target 仅支持 case/scenario")
    from app.services.api_ai_generate import generate_from_text

    try:
        result = await generate_from_text(
            db, project_id=payload.project_id, text=payload.text,
            target=payload.target, user_id=getattr(user, "id", None),
        )
    except ApiAiUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    from app.auth import log_audit
    await log_audit(
        db, getattr(user, "id", None), "ai.generate_from_text",
        details={"project_id": payload.project_id, "target": payload.target},
    )
    return result


@router.post("/ai/generate-data")
async def generate_data_endpoint(
    payload: GenerateDataRequest,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> dict:
    await _ensure_ai_enabled(db, user, payload.project_id, "data")
    definition = await db.get(db_models.ApiDefinition, payload.definition_id)
    if definition is None or definition.project_id != payload.project_id:
        raise HTTPException(status_code=404, detail="接口定义不存在")
    from app.services.api_ai_data import generate_data

    try:
        result = await generate_data(
            db, definition=definition, categories=payload.categories,
            row_count=payload.rows, save=payload.save, name=payload.name,
            project_id=payload.project_id, user_id=getattr(user, "id", None),
        )
    except ApiAiUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    from app.auth import log_audit
    await log_audit(
        db, getattr(user, "id", None), "ai.generate_data",
        details={"project_id": payload.project_id, "definition_id": payload.definition_id,
                 "saved": bool(payload.save), "rows": len(result.get("rows") or [])},
    )
    return result


@router.post("/ai/check-contract")
async def check_contract_endpoint(
    payload: CheckContractRequest,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> dict:
    run = await db.get(db_models.TestRun, payload.run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="运行记录不存在")
    project_id = await _run_project_id(db, run)
    if project_id is None:
        raise HTTPException(status_code=404, detail="运行记录未关联项目")
    await _ensure_ai_enabled(db, user, project_id, "contract")
    from app.services.api_ai_contract import check_contract

    try:
        row = await check_contract(
            db, run=run, project_id=project_id, definition_id=payload.definition_id,
            user_id=getattr(user, "id", None), force=payload.force,
        )
    except ApiAiUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return crud_api_ai.serialize_analysis(row)


@router.post("/ai/strategy")
async def strategy_endpoint(
    payload: StrategyRequest,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> dict:
    await _ensure_ai_enabled(db, user, payload.project_id, "strategy")
    from app.services.api_ai_strategy import generate_strategy

    try:
        row = await generate_strategy(
            db, project_id=payload.project_id,
            user_id=getattr(user, "id", None), force=payload.force,
            progress_id=payload.progress_id,
        )
    except ApiAiUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return crud_api_ai.serialize_analysis(row)


@router.get("/ai/strategy/status/{progress_id}")
async def strategy_status_endpoint(
    progress_id: str,
    user=Depends(get_current_user),
) -> dict:
    from app.services.api_ai_strategy import get_progress

    data = get_progress(progress_id)
    if data is None:
        return {"progress": 0, "message": "等待开始…"}
    return data


@router.get("/ai/analyses/{analysis_id}")
async def get_analysis_endpoint(
    analysis_id: int,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> dict:
    row = await db.get(db_models.ApiAiAnalysis, analysis_id)
    if row is None:
        raise HTTPException(status_code=404, detail="分析结果不存在")
    await _ensure_ai_enabled(db, user, row.project_id, "analysis")
    return crud_api_ai.serialize_analysis(row)
