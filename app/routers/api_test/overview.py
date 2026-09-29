"""接口测试首页统计。"""
from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import db_models
from app.auth import get_current_user, get_user_project_filter
from app.database import get_async_db
from app.tz import now as tz_now

router = APIRouter()


def _ensure_project_access(user, project_id: int) -> None:
    allowed = get_user_project_filter(user)
    if allowed is not None and project_id not in allowed:
        raise HTTPException(status_code=403, detail="无权访问该项目")


@router.get("/overview")
async def api_test_overview(
    project_id: int,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> dict:
    """接口数、用例数、场景数、覆盖率和近 7 天场景通过率。"""
    _ensure_project_access(user, project_id)
    definition_count = int(
        (
            await db.execute(
                select(func.count()).select_from(db_models.ApiDefinition).where(
                    db_models.ApiDefinition.project_id == project_id
                )
            )
        ).scalar()
        or 0
    )
    case_count = int(
        (
            await db.execute(
                select(func.count()).select_from(db_models.TestCase).where(
                    db_models.TestCase.project_id == project_id,
                    db_models.TestCase.case_kind == "api",
                )
            )
        ).scalar()
        or 0
    )
    scenario_count = int(
        (
            await db.execute(
                select(func.count()).select_from(db_models.ApiScenario).where(
                    db_models.ApiScenario.project_id == project_id
                )
            )
        ).scalar()
        or 0
    )
    covered_ids = set(
        (
            await db.execute(
                select(db_models.TestCase.api_definition_id).where(
                    db_models.TestCase.project_id == project_id,
                    db_models.TestCase.case_kind == "api",
                    db_models.TestCase.api_definition_id.is_not(None),
                )
            )
        ).scalars().all()
    )
    # 031（US11）：covered 也统一到**双来源引用**口径（旧的只看 api_definition_id 列会低估）
    covered = 0  # 由下方 coverage_items 计算后回填
    # 031（US11）：覆盖率双口径 —— 被引用（reference_count）+ 最近执行（last_status）
    from app.services.api_ai import build_coverage_items

    definition_rows = list(
        (
            await db.execute(
                select(db_models.ApiDefinition).where(
                    db_models.ApiDefinition.project_id == project_id
                )
            )
        ).scalars().all()
    )
    definitions = [
        {"id": d.id, "name": d.name, "method": d.method, "path": d.path}
        for d in definition_rows
    ]
    # 引用口径**双来源**：① TestCase.api_definition_id 列（编辑器「保存为用例」路径）
    #                     ② api_spec.steps[].definition_id（生成/导入路径写的是快照里的引用）
    # 只算列会漏掉 029 生成的用例 → 覆盖率被低估（实测暴露）。
    references: dict[int, int] = {}
    case_refs: dict[int, set[int]] = {}
    case_rows = (
        await db.execute(
            select(
                db_models.TestCase.id,
                db_models.TestCase.api_definition_id,
                db_models.TestCase.api_spec,
            ).where(
                db_models.TestCase.project_id == project_id,
                db_models.TestCase.case_kind == "api",
            )
        )
    ).all()
    for case_id, column_ref, spec in case_rows:
        refs: set[int] = set()
        if column_ref is not None:
            refs.add(int(column_ref))
        if isinstance(spec, dict):
            for step in spec.get("steps") or []:
                if not isinstance(step, dict):
                    continue
                raw = step.get("definition_id")
                try:
                    if raw is not None:
                        refs.add(int(raw))
                except (TypeError, ValueError):
                    continue
        case_refs[int(case_id)] = refs
        for def_id in refs:
            references[def_id] = references.get(def_id, 0) + 1
    # 最近执行：按 run id 倒序取每个 definition 的最近一条
    last_runs: dict[int, dict] = {}
    run_rows = (
        await db.execute(
            select(
                db_models.TestRun.case_id,
                db_models.TestRun.status,
                # TestRun 没有 created_at（模型见 app/models/batch.py）→ 用 start_time
                db_models.TestRun.start_time,
            )
            .where(db_models.TestRun.case_id.is_not(None))
            .order_by(db_models.TestRun.id.desc())
        )
    ).all()
    for case_id, status, started_at in run_rows:
        refs = case_refs.get(int(case_id)) if case_id is not None else None
        if not refs:
            continue
        for def_id in refs:
            if def_id in last_runs:
                continue
            last_runs[def_id] = {
                "status": status,
                "run_at": started_at.isoformat() if started_at else None,
            }
    coverage_items, coverage_gaps = build_coverage_items(definitions, references, last_runs)
    covered = sum(1 for item in coverage_items if item["referenced"])
    since = tz_now() - timedelta(days=7)
    batches = (
        await db.execute(
            select(db_models.RunBatch).where(
                db_models.RunBatch.project_id == project_id,
                db_models.RunBatch.source == "api_scenario",
                db_models.RunBatch.created_at >= since,
            )
        )
    ).scalars().all()
    passed = sum(int(b.passed or 0) for b in batches)
    failed = sum(int(b.failed or 0) for b in batches)
    finished = passed + failed
    return {
        "definition_count": definition_count,
        "case_count": case_count,
        "scenario_count": scenario_count,
        "covered_definitions": covered,
        "uncovered_definitions": max(0, definition_count - covered),
        "coverage": (covered / definition_count) if definition_count else None,
        "scenario_runs_7d": len(batches),
        "covered_by_last_run": sum(
            1 for i in coverage_items if i.get("last_status") == "passed"
        ),
        "coverage_items": coverage_items,
        "coverage_gaps": coverage_gaps,
        "scenario_passed_7d": passed,
        "scenario_failed_7d": failed,
        "scenario_pass_rate_7d": (passed / finished) if finished else None,
    }
