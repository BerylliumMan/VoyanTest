"""042 US2：Flaky 自动/显式执行的跳过语义。"""
from __future__ import annotations

import pytest

from app import db_models
from app.services.quality_flaky import filter_auto_cases


async def _make(db, status="quarantined"):
    project = db_models.Project(name="042 flaky skip")
    db.add(project)
    await db.flush()
    case = db_models.TestCase(
        project_id=project.id, project_case_number=1, name="隔离用例",
        case_kind="ui", priority="medium", status="active",
    )
    db.add(case)
    await db.flush()
    state = db_models.QualityFlakyState(
        project_id=project.id, case_id=case.id, status=status,
        sample_runs=5, passed=3, failed=2,
    )
    db.add(state)
    await db.commit()
    return project, case


@pytest.mark.asyncio
async def test_auto_source_skips_quarantined(db):
    _, case = await _make(db)
    kept, skipped = await filter_auto_cases(db, [case.id], "suite")
    assert kept == []
    assert skipped == 1


@pytest.mark.asyncio
async def test_explicit_source_keeps_quarantined(db):
    _, case = await _make(db)
    kept, skipped = await filter_auto_cases(db, [case.id], None)
    assert kept == [case.id]
    assert skipped == 0


@pytest.mark.asyncio
async def test_candidate_is_not_skipped(db):
    _, case = await _make(db, status="candidate")
    kept, skipped = await filter_auto_cases(db, [case.id], "scheduler")
    assert kept == [case.id]
    assert skipped == 0
