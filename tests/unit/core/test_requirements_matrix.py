"""042 US3：需求-用例矩阵计算。"""
from __future__ import annotations

import pytest

from app import db_models
from app.crud import quality as cq
from app.services.quality_requirements import matrix


@pytest.mark.asyncio
async def test_matrix_uncovered_and_linked(db):
    project = db_models.Project(name="042 matrix")
    db.add(project)
    await db.flush()
    req = db_models.QualityRequirement(project_id=project.id, name="登录")
    case = db_models.TestCase(
        project_id=project.id, project_case_number=1, name="登录用例",
        case_kind="ui", priority="medium", status="active",
    )
    db.add_all([req, case])
    await db.commit()

    result = await matrix(db, project_id=project.id)
    assert result["requirements"][0]["coverage"] == "uncovered"
    assert result["uncovered"] == [req.id]

    await cq.link_case(
        db, requirement_id=req.id, case_id=case.id, case_kind="ui",
    )
    result = await matrix(db, project_id=project.id)
    row = result["requirements"][0]
    assert row["case_count"] == 1
    assert row["coverage"] == "covered"


@pytest.mark.asyncio
async def test_matrix_type_filter(db):
    project = db_models.Project(name="042 matrix filter")
    db.add(project)
    await db.flush()
    req = db_models.QualityRequirement(project_id=project.id, name="订单")
    case = db_models.TestCase(
        project_id=project.id, project_case_number=1, name="接口用例",
        case_kind="api", priority="medium", status="active",
    )
    db.add_all([req, case])
    await db.commit()
    await cq.link_case(db, requirement_id=req.id, case_id=case.id, case_kind="api")

    ui = await matrix(db, project_id=project.id, case_kind="ui")
    assert ui["requirements"][0]["coverage"] == "uncovered"
    api = await matrix(db, project_id=project.id, case_kind="api")
    assert api["requirements"][0]["coverage"] == "covered"
