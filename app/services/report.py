"""
报告相关业务逻辑服务层。

将 ``app/routers/report_router.py`` 中散落的内联 SQLAlchemy 查询与
聚合/转换逻辑抽取为静态方法。Router 仅保留 HTTP 关注点（状态码、
响应格式化、依赖注入等），不再直接持有 ``db.query(...)`` 调用。

约束：
- 不引入 Pydantic 模型，方法返回值使用普通 Python 类型（dict / list）。
- 不引入 HTTP 依赖（``Depends`` / ``HTTPException``），权限不足时抛出
  :class:`ProjectAccessDenied` 之类的业务异常，由 router 转换为 HTTP 响应。
- 不修改 ``app/crud`` 中的现有函数，仅在上层做组合与数据塑形。
"""

from __future__ import annotations

import json
import logging
import os
from collections import defaultdict
from datetime import timedelta
from pathlib import Path
from typing import Any, Optional

from sqlalchemy.orm import Session

from app import crud, db_models
from app.auth import get_user_project_filter
from app.tz import now as tz_now

logger = logging.getLogger(__name__)


# 业务层异常（不依赖 fastapi/HTTPException），由 router 翻译为 HTTP 响应。
class ProjectAccessDenied(Exception):
    """当前用户无权访问指定项目或资源。"""


class BatchNotFound(Exception):
    """请求的运行批次不存在或当前用户无权查看。"""


# 报告文件根目录（路径穿越防护）
_REPORTS_ROOT = Path(os.path.abspath("reports"))


def _safe_report_path(report_path: str | None) -> Path | None:
    """校验并规范化报告路径，防止路径穿越攻击。

    返回 ``Path`` 指向已存在的文件，否则返回 ``None``。
    """
    if not report_path:
        return None
    resolved = Path(os.path.abspath(report_path))
    if not str(resolved).startswith(str(_REPORTS_ROOT)):
        logger.warning("路径穿越尝试: %s", report_path)
        return None
    return resolved if resolved.exists() else None


def _load_report_steps(report_path: str | None) -> list[Any]:
    """从报告 JSON 文件读取 ``steps`` 字段；路径不安全或读取失败时返回空列表。"""
    safe_path = _safe_report_path(report_path)
    if not safe_path:
        return []
    try:
        with open(safe_path, "r", encoding="utf-8") as f:
            report_data = json.load(f)
        return report_data.get("steps", []) or []
    except Exception:
        logger.warning("无法加载报告 JSON 文件: %s", report_path, exc_info=True)
        return []


def _batch_display_name(batch: db_models.RunBatch) -> str:
    """批次的对外显示名：优先使用自定义名称，否则使用创建时间。"""
    if batch.name:
        return batch.name
    if batch.created_at:
        return batch.created_at.strftime("%Y-%m-%d %H:%M")
    return ""


def _resolve_allowed_ids(user, project_id: Optional[int]) -> Optional[list[int]]:
    """计算 ``allowed_ids`` 并执行项目级别的存在性检查。

    - ``allowed_ids`` 为 ``None``：用户不受限。
    - ``allowed_ids`` 非空：用户仅能访问列表内项目；若指定 ``project_id``
      但不在 ``allowed_ids`` 内，抛出 :class:`ProjectAccessDenied`。
    """
    allowed_ids = get_user_project_filter(user)
    if allowed_ids is not None and project_id and project_id not in allowed_ids:
        raise ProjectAccessDenied("Project not found")
    return allowed_ids


class ReportService:
    """测试报告相关的业务逻辑聚合。

    所有方法均为 ``@staticmethod``，不持有状态。``db`` 始终是首参，
    其余参数来自 router 中的 query/path/body。
    """

    # ----------------------------
    # 统计 / 趋势
    # ----------------------------

    @staticmethod
    def get_statistics(
        db: Session,
        project_id: Optional[int],
        days: int,
        user: Any,
    ) -> dict[str, Any]:
        """返回统计信息的纯数据字典（不含 Pydantic 模型）。

        字段：
        ``total_cases``, ``total_runs``, ``today_runs``, ``passed``,
        ``failed``, ``skipped``, ``pass_rate``, ``success_rate``,
        ``avg_duration``。
        """
        end_date = tz_now()
        start_date = end_date - timedelta(days=days)

        allowed_ids = _resolve_allowed_ids(user, project_id)

        stats = crud.get_run_statistics(
            db,
            start_date=start_date,
            end_date=end_date,
            project_id=project_id,
            allowed_ids=allowed_ids,
        )

        if stats["total_batches"] == 0:
            return {
                "total_cases": 0,
                "total_runs": 0,
                "today_runs": 0,
                "passed": 0,
                "failed": 0,
                "skipped": 0,
                "pass_rate": 0.0,
                "success_rate": 0.0,
                "avg_duration": 0.0,
            }

        total_cases_in_batches = stats["total_cases_in_batches"]
        pass_rate = (
            round(stats["total_passed"] / total_cases_in_batches * 100, 2)
            if total_cases_in_batches > 0
            else 0.0
        )

        return {
            "total_cases": stats["total_cases"],
            "total_runs": total_cases_in_batches,
            "today_runs": stats["today_batches"],
            "passed": stats["total_passed"],
            "failed": stats["total_failed"],
            "skipped": 0,
            "pass_rate": pass_rate,
            "success_rate": pass_rate,
            "avg_duration": 0.0,
        }

    @staticmethod
    def get_trends(
        db: Session,
        project_id: Optional[int],
        days: int,
        user: Any,
    ) -> dict[str, Any]:
        """返回趋势数据的纯数据字典，包含 ``period`` 与 ``data`` 列表。"""
        end_date = tz_now()
        start_date = end_date - timedelta(days=days)

        allowed_ids = _resolve_allowed_ids(user, project_id)

        rows = crud.get_run_trends(
            db,
            start_date=start_date,
            end_date=end_date,
            project_id=project_id,
            allowed_ids=allowed_ids,
        )

        daily_data: dict[str, dict[str, int]] = defaultdict(
            lambda: {"passed": 0, "failed": 0, "total": 0}
        )
        for date, passed, failed, total_cases in rows:
            date_str = date.strftime("%Y-%m-%d") if hasattr(date, "strftime") else str(date)
            daily_data[date_str]["passed"] += passed or 0
            daily_data[date_str]["failed"] += failed or 0
            daily_data[date_str]["total"] += total_cases or 0

        data: list[dict[str, Any]] = []
        current_date = start_date
        while current_date <= end_date:
            date_str = current_date.strftime("%Y-%m-%d")
            bucket = daily_data.get(date_str, {"passed": 0, "failed": 0, "total": 0})
            total = bucket["total"]
            data.append(
                {
                    "date": date_str,
                    "passed": bucket["passed"],
                    "failed": bucket["failed"],
                    "skipped": 0,
                    "total": total,
                    "pass_rate": round(bucket["passed"] / total * 100, 2) if total > 0 else 0.0,
                }
            )
            current_date += timedelta(days=1)

        return {"period": f"{days}天", "data": data}

    # ----------------------------
    # 批次
    # ----------------------------

    @staticmethod
    def get_batches(
        db: Session,
        project_id: Optional[int],
        page: int,
        size: int,
        user: Any,
        status: Optional[str] = None,
    ) -> dict[str, Any]:
        """分页获取运行批次列表（包含项目名称解析）。"""
        allowed_ids = get_user_project_filter(user)
        filter_project_id = project_id
        if allowed_ids is not None:
            if project_id:
                if project_id not in allowed_ids:
                    raise ProjectAccessDenied("Project not found")
            else:
                # 未指定 project_id：让 crud 走 allowed_ids 列表过滤
                filter_project_id = None

        if allowed_ids is not None and not project_id:
            result = crud.list_run_batches(
                db,
                project_ids=allowed_ids,
                status=status,  # type: ignore[arg-type]
                page=page,
                size=size,
            )
        else:
            result = crud.list_run_batches(
                db,
                project_id=filter_project_id,  # type: ignore[arg-type]
                status=status,  # type: ignore[arg-type]
                page=page,
                size=size,
            )

        # 批量解析项目名（消除 N+1）
        project_ids = {b.project_id for b in result["items"] if b.project_id}
        projects: dict[int, db_models.Project] = {}
        if project_ids:
            for p in db.query(db_models.Project).filter(
                db_models.Project.id.in_(project_ids)
            ).all():
                projects[p.id] = p

        items = []
        for batch in result["items"]:
            project = projects.get(batch.project_id)
            items.append(
                {
                    "id": batch.id,
                    "name": _batch_display_name(batch),
                    "project_id": batch.project_id,
                    "project_name": project.name if project else "",
                    "status": batch.status,
                    "total_cases": batch.total_cases,
                    "passed": batch.passed,
                    "failed": batch.failed,
                    "created_at": batch.created_at.isoformat() if batch.created_at else None,
                    "started_at": batch.started_at.isoformat() if batch.started_at else None,
                    "finished_at": batch.finished_at.isoformat() if batch.finished_at else None,
                }
            )

        return {
            "total": result["total"],
            "page": result["page"],
            "size": result["size"],
            "items": items,
        }

    @staticmethod
    def export_batch_report(
        db: Session,
        batch_id: int,
        user: Any,
    ) -> dict[str, Any]:
        """构造批次报告的导出数据（与 ``/batches/{batch_id}`` 详情一致）。

        与详情端点不同，导出还需要从各 run 的 ``report.json`` 中读取步骤
        数据，因此权限校验通过后展开 runs 的步骤。
        """
        batch = crud.get_run_batch(db, batch_id)
        if not batch:
            raise BatchNotFound("Batch not found")

        allowed_ids = get_user_project_filter(user)
        if allowed_ids is not None and batch.project_id not in allowed_ids:
            raise BatchNotFound("Batch not found")

        # 动态计算状态（可能修复卡死的 pending 记录）
        crud._compute_batch_status(db, batch)

        project = (
            db.query(db_models.Project)
            .filter(db_models.Project.id == batch.project_id)
            .first()
        )
        project_name = project.name if project else ""

        related = crud.get_batch_detail_with_related(db, batch_id)
        runs = related["runs"]
        cases = related["cases"]

        runs_data: list[dict[str, Any]] = []
        for run in runs:
            case = cases.get(run.case_id)
            case_name = case.name if case else ""
            runs_data.append(
                {
                    "run_id": run.id,
                    "case_id": run.case_id,
                    "case_name": case_name,
                    "status": run.status,
                    "duration": run.duration,
                    "started_at": run.start_time.isoformat() if run.start_time else None,
                    "finished_at": run.end_time.isoformat() if run.end_time else None,
                    "steps": _load_report_steps(run.report_path),
                }
            )

        return {
            "id": batch.id,
            "name": _batch_display_name(batch),
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
