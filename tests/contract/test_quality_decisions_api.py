"""042：质量决策端点契约（基础权限/空数据/需求矩阵）。"""
from __future__ import annotations

import pytest

from app import db_models


@pytest.fixture(autouse=True)
def _mark_db_ready(db):
    import app.database as db_mod

    previous = db_mod.AsyncSessionLocal
    wrapper = db_mod._LazySessionMaker()
    wrapper.configure(previous)
    db_mod.AsyncSessionLocal = wrapper
    yield
    db_mod.AsyncSessionLocal = previous


async def _project(db, **kwargs):
    project = db_models.Project(name="042 质量决策项目", **kwargs)
    db.add(project)
    await db.commit()
    await db.refresh(project)
    return project


@pytest.mark.asyncio
async def test_impact_analysis_empty_project(client, db, admin_cookies):
    project = await _project(db)
    resp = client.post(
        "/api/quality/impact-analysis",
        json={"project_id": project.id}, cookies=admin_cookies,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["candidates"] == []


@pytest.mark.asyncio
async def test_requirements_create_and_matrix(client, db, admin_cookies):
    project = await _project(db)
    created = client.post(
        "/api/quality/requirements",
        json={"project_id": project.id, "name": "登录"}, cookies=admin_cookies,
    )
    assert created.status_code == 200, created.text
    dup = client.post(
        "/api/quality/requirements",
        json={"project_id": project.id, "name": "登录"}, cookies=admin_cookies,
    )
    assert dup.status_code == 400
    matrix = client.get(
        f"/api/quality/requirements/matrix?project_id={project.id}",
        cookies=admin_cookies,
    )
    assert matrix.status_code == 200, matrix.text
    assert len(matrix.json()["requirements"]) == 1


@pytest.mark.asyncio
async def test_flaky_list_empty(client, db, admin_cookies):
    project = await _project(db)
    resp = client.get(
        f"/api/quality/flaky?project_id={project.id}", cookies=admin_cookies,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["items"] == []


@pytest.mark.asyncio
async def test_impact_403_when_disabled(client, db, admin_cookies):
    project = await _project(db, ai_settings={"quality_impact_enabled": False})
    resp = client.post(
        "/api/quality/impact-analysis",
        json={"project_id": project.id}, cookies=admin_cookies,
    )
    assert resp.status_code == 403


async def _case(db, project, name="042 回归用例"):
    case = db_models.TestCase(
        project_id=project.id, project_case_number=1, name=name,
        case_kind="ui", priority="medium", status="active",
    )
    db.add(case)
    await db.commit()
    await db.refresh(case)
    return case


@pytest.mark.asyncio
async def test_regression_plan_crud(client, db, admin_cookies):
    project = await _project(db)
    case = await _case(db, project)
    empty = client.post(
        "/api/quality/regression-plans",
        json={"project_id": project.id, "name": "空计划", "items": []},
        cookies=admin_cookies,
    )
    assert empty.status_code == 400
    created = client.post(
        "/api/quality/regression-plans",
        json={"project_id": project.id, "name": "回归计划",
              "items": [{"case_id": case.id, "case_kind": "ui"}]},
        cookies=admin_cookies,
    )
    assert created.status_code == 200, created.text
    plan_id = created.json()["id"]
    listed = client.get(
        f"/api/quality/regression-plans?project_id={project.id}", cookies=admin_cookies,
    )
    assert listed.status_code == 200 and len(listed.json()["items"]) == 1
    detail = client.get(f"/api/quality/regression-plans/{plan_id}", cookies=admin_cookies)
    assert detail.status_code == 200
    deleted = client.delete(f"/api/quality/regression-plans/{plan_id}", cookies=admin_cookies)
    assert deleted.status_code == 200
    assert client.get(f"/api/quality/regression-plans/{plan_id}", cookies=admin_cookies).status_code == 404


@pytest.mark.asyncio
async def test_flaky_endpoints_404_and_list(client, db, admin_cookies):
    project = await _project(db)
    listed = client.get(f"/api/quality/flaky?project_id={project.id}", cookies=admin_cookies)
    assert listed.status_code == 200
    missing = client.post(
        "/api/quality/flaky/999999/quarantine",
        json={"reason": "test"}, cookies=admin_cookies,
    )
    assert missing.status_code == 404
