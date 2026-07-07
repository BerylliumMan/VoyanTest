"""
测试报告 API 路由
提供测试统计、趋势分析、报告查询等功能
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from fastapi import APIRouter, HTTPException, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import SQLAlchemyError
from datetime import datetime
from app.tz import now as tz_now
from typing import List, Optional
from pydantic import BaseModel, Field

from ..database import get_async_db
from ..auth import require_admin, get_current_user, get_user_project_filter
from .. import crud
from ..services import ReportService
from ..services.report import BatchNotFound, ProjectAccessDenied

logger = logging.getLogger(__name__)


class BatchUpdate(BaseModel):
    """Request body for updating a batch name."""
    name: str = Field(..., min_length=1, max_length=200, description="New batch name")


# 允许的报告根目录（路径穿越防护）
_REPORTS_ROOT = Path(os.path.abspath("reports"))


def _read_json(path: Path) -> dict:
    """同步读取并解析 JSON 文件，供 asyncio.to_thread 使用。"""
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _safe_report_path(report_path: str | None) -> Path | None:
    """校验并规范化报告路径，防止路径穿越攻击。"""
    if not report_path:
        return None
    resolved = Path(os.path.abspath(report_path))
    if not str(resolved).startswith(str(_REPORTS_ROOT)):
        logger.warning("路径穿越尝试: %s", report_path)
        return None
    return resolved if resolved.exists() else None


router = APIRouter(
    prefix="/api/reports",
    tags=["测试报告"]
)


# ==================== Pydantic 模型 ====================

class TestStatistics(BaseModel):
    """测试统计"""
    total_cases: int = Field(..., description="总用例数")
    total_runs: int = Field(..., description="总执行次数")
    today_runs: int = Field(0, description="今日执行次数")
    passed: int = Field(..., description="通过次数")
    failed: int = Field(..., description="失败次数")
    skipped: int = Field(..., description="跳过次数")
    pass_rate: float = Field(..., description="通过率")
    success_rate: float = Field(..., description="成功率（同 pass_rate）")
    avg_duration: float = Field(..., description="平均执行时长(秒)")


class TrendDataPoint(BaseModel):
    """趋势数据点"""
    date: str = Field(..., description="日期")
    passed: int = Field(..., description="通过数")
    failed: int = Field(..., description="失败数")
    skipped: int = Field(..., description="跳过数")
    total: int = Field(..., description="总数")
    pass_rate: float = Field(..., description="通过率")


class TestTrend(BaseModel):
    """测试趋势"""
    period: str = Field(..., description="周期")
    data: List[TrendDataPoint] = Field(..., description="趋势数据")


class ReportSummary(BaseModel):
    """报告摘要"""
    statistics: TestStatistics = Field(..., description="统计信息")
    trends: TestTrend = Field(..., description="趋势信息")
    recent_runs: List[dict] = Field(..., description="最近执行")


class RunDetail(BaseModel):
    """执行详情"""
    run_id: int = Field(..., description="执行ID")
    case_id: int = Field(..., description="用例ID")
    case_name: str = Field(..., description="用例名称")
    status: str = Field(..., description="状态")
    start_time: Optional[datetime] = Field(None, description="开始时间（pending 时为 null）")
    end_time: Optional[datetime] = Field(None, description="结束时间（pending/running 时为 null）")
    duration: float = Field(..., description="执行时长")
    report_path: Optional[str] = Field(None, description="报告路径")
    log_path: Optional[str] = Field(None, description="日志路径")


# ==================== API 路由 ====================

@router.get("/statistics", response_model=TestStatistics)
async def get_test_statistics(
    project_id: Optional[int] = None,
    days: int = 30,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db)
) -> TestStatistics:
    """获取测试统计信息（基于批次）"""
    try:
        data = await ReportService.get_statistics(db, project_id, days, user)
    except ProjectAccessDenied as e:
        raise HTTPException(status_code=404, detail=str(e))
    return TestStatistics(**data)


@router.get("/trends", response_model=TestTrend)
async def get_test_trends(
    project_id: Optional[int] = None,
    days: int = 30,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db)
) -> TestTrend:
    """获取测试趋势数据（基于批次）"""
    try:
        data = await ReportService.get_trends(db, project_id, days, user)
    except ProjectAccessDenied as e:
        raise HTTPException(status_code=404, detail=str(e))
    return TestTrend(
        period=data["period"],
        data=[TrendDataPoint(**point) for point in data["data"]],
    )


@router.get("/summary", response_model=ReportSummary)
async def get_report_summary(
    project_id: Optional[int] = None,
    days: int = 30,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db)
) -> ReportSummary:
    """
    获取报告摘要（统计 + 趋势 + 最近执行）
    """
    try:
        statistics_data = await ReportService.get_statistics(db, project_id, days, user)
        trends_data = await ReportService.get_trends(db, project_id, days, user)
    except ProjectAccessDenied as e:
        raise HTTPException(status_code=404, detail=str(e))

    allowed_ids = get_user_project_filter(user)
    if allowed_ids is not None and project_id and project_id not in allowed_ids:
        raise HTTPException(status_code=404, detail="Project not found")

    recent_rows = await crud.list_recent_runs(
        db,
        limit=10,
        project_id=project_id,
        allowed_ids=allowed_ids,
    )

    recent_runs = []
    for run, case_name in recent_rows:
        recent_runs.append({
            "run_id": run.id,
            "case_id": run.case_id,
            "case_name": case_name,
            "status": run.status,
            "start_time": run.start_time.isoformat() if run.start_time else None,
            "duration": run.duration
        })

    return ReportSummary(
        statistics=TestStatistics(**statistics_data),
        trends=TestTrend(
            period=trends_data["period"],
            data=[TrendDataPoint(**point) for point in trends_data["data"]],
        ),
        recent_runs=recent_runs
    )


@router.get("/runs/{run_id}")
async def get_run_detail(run_id: int, user=Depends(get_current_user), db: AsyncSession = Depends(get_async_db)) -> dict:
    """
    获取单次执行详情，包含步骤数据（从 report JSON 读取）。
    """
    result = await crud.get_run_detail_with_case(db, run_id)
    if not result:
        raise HTTPException(status_code=404, detail="执行记录不存在")

    run, case_name, case_project_id = result

    allowed_ids = get_user_project_filter(user)
    if allowed_ids is not None and case_project_id not in allowed_ids:
        raise HTTPException(status_code=404, detail="执行记录不存在")

    response = {
        "run_id": run.id,
        "case_id": run.case_id,
        "case_name": case_name,
        "status": run.status,
        "start_time": run.start_time.isoformat() if run.start_time else None,
        "end_time": run.end_time.isoformat() if run.end_time else None,
        "duration": run.duration,
        "report_path": run.report_path,
        "log_path": run.log_path,
        "steps": []
    }

    # 从 report JSON 文件加载步骤数据（含路径穿越防护）
    safe_path = _safe_report_path(run.report_path)
    if safe_path:
        try:
            report_data = await asyncio.to_thread(_read_json, safe_path)
            response["steps"] = report_data.get("steps", [])
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            # 文件 I/O / JSON 损坏 / 编码错误都降级为空 steps
            logger.warning("无法加载报告 JSON 文件: %s", run.report_path, exc_info=True)

    return response


@router.get("/runs")
async def list_runs(
    project_id: Optional[int] = None,
    status: Optional[str] = None,
    page: int = 1,
    size: int = 20,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db)
) -> dict:
    """
    获取执行记录列表
    """
    allowed_ids = get_user_project_filter(user)
    if allowed_ids is not None and project_id and project_id not in allowed_ids:
        raise HTTPException(status_code=404, detail="Project not found")

    result = await crud.list_runs_with_case(
        db,
        project_id=project_id,
        status=status,
        allowed_ids=allowed_ids,
        page=page,
        size=size,
    )

    items = []
    for run, case_name in result["items"]:
        items.append({
            "run_id": run.id,
            "case_id": run.case_id,
            "case_name": case_name,
            "status": run.status,
            "start_time": run.start_time.isoformat() if run.start_time else None,
            "duration": run.duration
        })

    return {
        "total": result["total"],
        "page": page,
        "size": size,
        "items": items
    }


# ==================== 批次报告 API ====================

@router.get("/batches")
async def list_batches(
    project_id: Optional[int] = None,
    status: Optional[str] = None,
    page: int = 1,
    size: int = 20,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db)
) -> dict:
    """获取运行批次列表（分页）"""
    try:
        return await ReportService.get_batches(db, project_id, page, size, user, status=status)
    except ProjectAccessDenied as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/batches/{batch_id}")
async def get_batch_detail(batch_id: int, user=Depends(get_current_user), db: AsyncSession = Depends(get_async_db)) -> dict:
    """获取批次详情，包含所有用例运行结果"""
    batch = await crud.get_run_batch(db, batch_id)
    if not batch:
        raise HTTPException(status_code=404, detail="Batch not found")

    allowed_ids = get_user_project_filter(user)
    if allowed_ids is not None and batch.project_id not in allowed_ids:
        raise HTTPException(status_code=404, detail="Batch not found")

    # 动态计算状态
    await crud._compute_batch_status(db, batch)

    project = await crud.get_project(db, batch.project_id)
    project_name = project.name if project else ""

    related = await crud.get_batch_detail_with_related(db, batch_id)
    runs = related["runs"]
    cases = related["cases"]

    runs_data = []
    for run in runs:
        case = cases.get(run.case_id)
        case_name = case.name if case else ""

        run_info = {
            "run_id": run.id,
            "case_id": run.case_id,
            "case_name": case_name,
            "status": run.status,
            "duration": run.duration,
            "started_at": run.start_time.isoformat() if run.start_time else None,
            "finished_at": run.end_time.isoformat() if run.end_time else None,
            "steps": [],
        }

        # 从 report.json 读取步骤数据（含路径穿越防护）
        safe_path = _safe_report_path(run.report_path)
        if safe_path:
            try:
                report_data = await asyncio.to_thread(_read_json, safe_path)
                run_info["steps"] = report_data.get("steps", [])
            except (OSError, json.JSONDecodeError, UnicodeDecodeError):
                # 文件 I/O / JSON 损坏 / 编码错误都降级为空 steps
                logger.warning("无法加载批次报告 JSON 文件: %s", run.report_path, exc_info=True)

        runs_data.append(run_info)

    return {
        "id": batch.id,
        "name": batch.name or batch.created_at.strftime("%Y-%m-%d %H:%M") if batch.created_at else "",
        "project_id": batch.project_id,
        "project_name": project_name,
        "status": batch.status,
        "total_cases": batch.total_cases,
        "passed": batch.passed,
        "failed": batch.failed,
        "created_at": batch.created_at.isoformat() if batch.created_at else None,
        "started_at": batch.started_at.isoformat() if batch.started_at else None,
        "finished_at": batch.finished_at.isoformat() if batch.finished_at else None,
        "runs": runs_data,
    }


@router.put("/batches/{batch_id}")
async def update_batch(batch_id: int, body: BatchUpdate, admin=Depends(require_admin), db: AsyncSession = Depends(get_async_db)) -> dict:
    """更新批次名称"""
    batch = await crud.update_run_batch(db, batch_id, name=body.name)
    if not batch:
        raise HTTPException(status_code=404, detail="Batch not found")

    return {
        "id": batch.id,
        "name": batch.name,
        "status": batch.status,
        "total_cases": batch.total_cases,
        "passed": batch.passed,
        "failed": batch.failed,
    }


@router.get("/batches/{batch_id}/export")
async def export_batch(batch_id: int, user=Depends(get_current_user), db: AsyncSession = Depends(get_async_db)) -> JSONResponse:
    """导出批次报告为 JSON 文件"""
    try:
        detail = await ReportService.export_batch_report(db, batch_id, user)
    except BatchNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))

    filename = f"batch_{batch_id}_{tz_now().strftime('%Y%m%d_%H%M%S')}.json"
    response = JSONResponse(content=detail)
    response.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


@router.delete("/batches/{batch_id}")
async def delete_batch(batch_id: int, user=Depends(get_current_user), db: AsyncSession = Depends(get_async_db)) -> dict:
    """删除运行批次及其关联数据"""
    batch = await crud.get_run_batch(db, batch_id)
    if not batch:
        raise HTTPException(status_code=404, detail="Batch not found")
    allowed_ids = get_user_project_filter(user)
    if allowed_ids is not None and batch.project_id not in allowed_ids:
        raise HTTPException(status_code=403, detail="无权操作该批次")
    try:
        success = await crud.delete_run_batch(db, batch_id)
        if not success:
            raise HTTPException(status_code=404, detail="Batch not found")
        return {"message": "Batch deleted"}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Delete batch %s failed", batch_id)
        raise HTTPException(status_code=500, detail=f"Failed to delete batch: {e}")


@router.post("/compare")
async def compare_batches(
    batch_a: int,
    batch_b: int,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> dict:
    """对比两个批次的运行结果。"""
    from app.crud.run import get_run_batch

    a = await get_run_batch(db, batch_a)
    b = await get_run_batch(db, batch_b)
    if not a or not b:
        raise HTTPException(status_code=404, detail="Batch not found")

    return {
        "a": {"id": a.id, "name": a.name, "status": a.status, "passed": a.passed, "failed": a.failed, "total": a.total_cases},
        "b": {"id": b.id, "name": b.name, "status": b.status, "passed": b.passed, "failed": b.failed, "total": b.total_cases},
        "passed_diff": (b.passed or 0) - (a.passed or 0),
        "failed_diff": (b.failed or 0) - (a.failed or 0),
    }
