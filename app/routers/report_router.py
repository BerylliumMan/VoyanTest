"""
测试报告 API 路由
提供测试统计、趋势分析、报告查询等功能
"""

import json
import logging
import os
from pathlib import Path
from fastapi import APIRouter, HTTPException, Depends
from sqlalchemy.orm import Session
from sqlalchemy import func
from datetime import datetime, timedelta
from app.tz import now as tz_now
from typing import List, Optional
from pydantic import BaseModel, Field

from ..database import get_db
from ..auth import require_admin
from .. import crud, db_models

logger = logging.getLogger(__name__)

# 允许的报告根目录（路径穿越防护）
_REPORTS_ROOT = Path(os.path.abspath("reports"))


def _safe_report_path(report_path: str | None) -> Path | None:
    """校验并规范化报告路径，防止路径穿越攻击。"""
    if not report_path:
        return None
    resolved = Path(os.path.abspath(report_path))
    if not str(resolved).startswith(str(_REPORTS_ROOT)):
        logger.warning(f"路径穿越尝试: {report_path}")
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
def get_test_statistics(
    project_id: Optional[int] = None,
    days: int = 30,
    db: Session = Depends(get_db)
):
    """获取测试统计信息（基于批次）"""
    end_date = tz_now()
    start_date = end_date - timedelta(days=days)
    query = db.query(db_models.RunBatch)

    if project_id:
        query = query.filter(db_models.RunBatch.project_id == project_id)

    query = query.filter(
        db_models.RunBatch.created_at >= start_date,
        db_models.RunBatch.created_at <= end_date
    )

    total_batches = query.count()

    if total_batches == 0:
        return TestStatistics(
            total_cases=0,
            total_runs=0,
            today_runs=0,
            passed=0,
            failed=0,
            skipped=0,
            pass_rate=0.0,
            success_rate=0.0,
            avg_duration=0.0
        )

    # 汇总所有批次的用例计数
    result = query.with_entities(
        func.sum(db_models.RunBatch.total_cases),
        func.sum(db_models.RunBatch.passed),
        func.sum(db_models.RunBatch.failed),
    ).first()

    total_cases_in_batches = result[0] or 0
    total_passed = result[1] or 0
    total_failed = result[2] or 0

    # 今日执行批次数
    today_start = tz_now().replace(hour=0, minute=0, second=0, microsecond=0)
    today_batches = query.filter(db_models.RunBatch.created_at >= today_start).count()

    # 总用例数（测试用例表）
    total_cases_query = db.query(db_models.TestCase)
    if project_id:
        total_cases_query = total_cases_query.filter(
            db_models.TestCase.project_id == project_id
        )
    total_cases = total_cases_query.count()

    pass_rate = round(total_passed / total_cases_in_batches * 100, 2) if total_cases_in_batches > 0 else 0.0

    return TestStatistics(
        total_cases=total_cases,
        total_runs=total_cases_in_batches,
        today_runs=today_batches,
        passed=total_passed,
        failed=total_failed,
        skipped=0,
        pass_rate=pass_rate,
        success_rate=pass_rate,
        avg_duration=0.0
    )


@router.get("/trends", response_model=TestTrend)
def get_test_trends(
    project_id: Optional[int] = None,
    days: int = 30,
    db: Session = Depends(get_db)
):
    """获取测试趋势数据（基于批次）"""
    end_date = tz_now()
    start_date = end_date - timedelta(days=days)

    query = db.query(
        func.date(db_models.RunBatch.created_at).label('date'),
        db_models.RunBatch.passed,
        db_models.RunBatch.failed,
        db_models.RunBatch.total_cases,
    ).filter(
        db_models.RunBatch.created_at >= start_date,
        db_models.RunBatch.created_at <= end_date
    )

    if project_id:
        query = query.filter(db_models.RunBatch.project_id == project_id)

    results = query.all()

    from collections import defaultdict
    daily_data = defaultdict(lambda: {'passed': 0, 'failed': 0, 'total': 0})

    for date, passed, failed, total_cases in results:
        date_str = date.strftime('%Y-%m-%d') if hasattr(date, 'strftime') else str(date)
        daily_data[date_str]['passed'] += passed or 0
        daily_data[date_str]['failed'] += failed or 0
        daily_data[date_str]['total'] += total_cases or 0

    trend_data = []
    current_date = start_date

    while current_date <= end_date:
        date_str = current_date.strftime('%Y-%m-%d')
        data = daily_data.get(date_str, {'passed': 0, 'failed': 0, 'total': 0})

        trend_data.append(TrendDataPoint(
            date=date_str,
            passed=data['passed'],
            failed=data['failed'],
            skipped=0,
            total=data['total'],
            pass_rate=round(data['passed'] / data['total'] * 100, 2) if data['total'] > 0 else 0.0
        ))

        current_date += timedelta(days=1)

    return TestTrend(
        period=f"{days}天",
        data=trend_data
    )


@router.get("/summary", response_model=ReportSummary)
def get_report_summary(
    project_id: Optional[int] = None,
    days: int = 30,
    db: Session = Depends(get_db)
):
    """
    获取报告摘要（统计 + 趋势 + 最近执行）
    """
    # 获取统计
    statistics = get_test_statistics(project_id, days, db)
    
    # 获取趋势
    trends = get_test_trends(project_id, days, db)
    
    # 获取最近执行
    query = db.query(
        db_models.TestRun,
        db_models.TestCase.name
    ).join(
        db_models.TestCase,
        db_models.TestRun.case_id == db_models.TestCase.id
    ).order_by(
        db_models.TestRun.start_time.desc()
    ).limit(10)
    
    if project_id:
        query = query.filter(db_models.TestCase.project_id == project_id)
    
    recent_runs = []
    for run, case_name in query.all():
        recent_runs.append({
            "run_id": run.id,
            "case_id": run.case_id,
            "case_name": case_name,
            "status": run.status,
            "start_time": run.start_time.isoformat() if run.start_time else None,
            "duration": run.duration
        })
    
    return ReportSummary(
        statistics=statistics,
        trends=trends,
        recent_runs=recent_runs
    )


@router.get("/runs/{run_id}")
def get_run_detail(run_id: int, db: Session = Depends(get_db)):
    """
    获取单次执行详情，包含步骤数据（从 report JSON 读取）。
    """
    result = db.query(
        db_models.TestRun,
        db_models.TestCase.name
    ).join(
        db_models.TestCase,
        db_models.TestRun.case_id == db_models.TestCase.id
    ).filter(
        db_models.TestRun.id == run_id
    ).first()

    if not result:
        raise HTTPException(status_code=404, detail="执行记录不存在")

    run, case_name = result

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
            with open(safe_path, "r", encoding="utf-8") as f:
                report_data = json.load(f)
            response["steps"] = report_data.get("steps", [])
        except Exception:
            logger.warning(f"无法加载报告 JSON 文件: {run.report_path}", exc_info=True)

    return response


@router.get("/runs")
def list_runs(
    project_id: Optional[int] = None,
    status: Optional[str] = None,
    page: int = 1,
    size: int = 20,
    db: Session = Depends(get_db)
):
    """
    获取执行记录列表
    """
    query = db.query(
        db_models.TestRun,
        db_models.TestCase.name
    ).join(
        db_models.TestCase,
        db_models.TestRun.case_id == db_models.TestCase.id
    )

    if project_id:
        query = query.filter(db_models.TestCase.project_id == project_id)

    if status:
        query = query.filter(db_models.TestRun.status == status)

    # 分页
    total = query.count()
    offset = (page - 1) * size
    results = query.order_by(
        db_models.TestRun.start_time.desc()
    ).offset(offset).limit(size).all()

    items = []
    for run, case_name in results:
        items.append({
            "run_id": run.id,
            "case_id": run.case_id,
            "case_name": case_name,
            "status": run.status,
            "start_time": run.start_time.isoformat() if run.start_time else None,
            "duration": run.duration
        })

    return {
        "total": total,
        "page": page,
        "size": size,
        "items": items
    }


# ==================== 批次报告 API ====================

@router.get("/batches")
def list_batches(
    project_id: Optional[int] = None,
    status: Optional[str] = None,
    page: int = 1,
    size: int = 20,
    db: Session = Depends(get_db)
):
    """获取运行批次列表（分页）"""
    result = crud.list_run_batches(db, project_id=project_id, status=status, page=page, size=size)

    items = []
    for batch in result["items"]:
        # 获取项目名称
        project = db.query(db_models.Project).filter(db_models.Project.id == batch.project_id).first()
        project_name = project.name if project else ""

        items.append({
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
        })

    return {
        "total": result["total"],
        "page": result["page"],
        "size": result["size"],
        "items": items,
    }


@router.get("/batches/{batch_id}")
def get_batch_detail(batch_id: int, db: Session = Depends(get_db)):
    """获取批次详情，包含所有用例运行结果"""
    batch = crud.get_run_batch(db, batch_id)
    if not batch:
        raise HTTPException(status_code=404, detail="Batch not found")

    # 动态计算状态
    crud._compute_batch_status(db, batch)

    project = db.query(db_models.Project).filter(db_models.Project.id == batch.project_id).first()
    project_name = project.name if project else ""

    # 获取批次下所有运行
    runs = db.query(db_models.TestRun).filter(db_models.TestRun.batch_id == batch_id).all()

    runs_data = []
    for run in runs:
        case = db.query(db_models.TestCase).filter(db_models.TestCase.id == run.case_id).first()
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
                with open(safe_path, "r", encoding="utf-8") as f:
                    report_data = json.load(f)
                run_info["steps"] = report_data.get("steps", [])
            except Exception:
                logger.warning(f"无法加载批次报告 JSON 文件: {run.report_path}", exc_info=True)

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
def update_batch(batch_id: int, body: dict, admin=Depends(require_admin), db: Session = Depends(get_db)):
    """更新批次名称"""
    batch = crud.update_run_batch(db, batch_id, name=body.get("name"))
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
def export_batch(batch_id: int, db: Session = Depends(get_db)):
    """导出批次报告为 JSON 文件"""
    detail = get_batch_detail(batch_id, db)

    from fastapi.responses import JSONResponse

    filename = f"batch_{batch_id}_{tz_now().strftime('%Y%m%d_%H%M%S')}.json"
    response = JSONResponse(content=detail)
    response.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


@router.delete("/batches/{batch_id}")
def delete_batch(batch_id: int, db: Session = Depends(get_db)):
    """删除运行批次及其关联数据"""
    try:
        success = crud.delete_run_batch(db, batch_id)
        if not success:
            raise HTTPException(status_code=404, detail="Batch not found")
        return {"message": "Batch deleted"}
    except Exception as e:
        logger.error(f"Delete batch {batch_id} failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
