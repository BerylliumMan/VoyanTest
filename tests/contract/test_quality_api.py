"""041：质量 AI 端点契约（权限/降级/404）。"""
from __future__ import annotations

import pytest

from app import db_models
from app.services.api_ai import ApiAiUnavailable


@pytest.fixture(autouse=True)
def _mark_db_ready(db):
    import app.database as db_mod

    previous = db_mod.AsyncSessionLocal
    wrapper = db_mod._LazySessionMaker()
    wrapper.configure(previous)
    db_mod.AsyncSessionLocal = wrapper
    yield
    db_mod.AsyncSessionLocal = previous


async def _project(db, *, ai_settings=None):
    project = db_models.Project(name="质量 AI 契约项目", ai_settings=ai_settings)
    db.add(project)
    await db.commit()
    await db.refresh(project)
    return project


async def _run(db, project, status="failed"):
    case = db_models.TestCase(
        project_id=project.id, project_case_number=0, name="质量 AI 用例",
        case_kind="ui", priority="medium", status="active",
    )
    db.add(case)
    await db.flush()
    run = db_models.TestRun(case_id=case.id, status=status)
    db.add(run)
    await db.commit()
    return case, run


@pytest.mark.asyncio
async def test_failure_analysis_404_unknown_run(client, admin_cookies):
    resp = client.post(
        "/api/quality/failure-analysis", json={"run_id": -1}, cookies=admin_cookies,
    )
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_failure_analysis_400_for_passed_run(client, db, admin_cookies):
    project = await _project(db)
    _, run = await _run(db, project, status="passed")
    resp = client.post(
        "/api/quality/failure-analysis", json={"run_id": run.id}, cookies=admin_cookies,
    )
    assert resp.status_code == 400, resp.text


@pytest.mark.asyncio
async def test_failure_analysis_403_when_project_disabled(client, db, admin_cookies):
    project = await _project(db, ai_settings={"quality_ai_enabled": False})
    _, run = await _run(db, project)
    resp = client.post(
        "/api/quality/failure-analysis", json={"run_id": run.id}, cookies=admin_cookies,
    )
    assert resp.status_code == 403, resp.text


@pytest.mark.asyncio
async def test_failure_analysis_503_when_unavailable(client, db, admin_cookies, monkeypatch):
    project = await _project(db)
    _, run = await _run(db, project)

    async def boom(*args, **kwargs):
        raise ApiAiUnavailable("模型不可用")

    monkeypatch.setattr("app.routers.quality.analysis.analyze_ui_failure", boom)
    resp = client.post(
        "/api/quality/failure-analysis", json={"run_id": run.id}, cookies=admin_cookies,
    )
    assert resp.status_code == 503, resp.text
    assert "模型不可用" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_review_404_unknown_draft(client, db, admin_cookies):
    project = await _project(db)
    resp = client.post(
        "/api/quality/case-reviews",
        json={"project_id": project.id, "target_type": "draft", "target_ids": [999999]},
        cookies=admin_cookies,
    )
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_defect_404_unknown_run(client, admin_cookies):
    resp = client.post(
        "/api/quality/defect-reports", json={"project_id": 1, "run_id": 999999},
        cookies=admin_cookies,
    )
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_review_single_200(client, db, admin_cookies, monkeypatch):
    project = await _project(db)
    row = db_models.ApiAiAnalysis(
        project_id=project.id, kind="review", status="ready",
        input_hash="x", result={"summary": "ok", "items": []},
    )
    db.add(row)
    await db.commit()

    async def fake_review(*args, **kwargs):
        return row, False

    monkeypatch.setattr("app.routers.quality.review.review_cases", fake_review)
    resp = client.post(
        "/api/quality/case-reviews",
        json={"project_id": project.id, "target_type": "draft", "target_ids": [1]},
        cookies=admin_cookies,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["kind"] == "review"


@pytest.mark.asyncio
async def test_review_batch_202(client, db, admin_cookies, monkeypatch):
    project = await _project(db)
    monkeypatch.setattr(
        "app.services.quality_tasks.start_review_task",
        lambda **kwargs: "qrev_test",
    )
    resp = client.post(
        "/api/quality/case-reviews",
        json={"project_id": project.id, "target_type": "draft", "target_ids": [1, 2]},
        cookies=admin_cookies,
    )
    assert resp.status_code == 202, resp.text
    assert resp.json()["task_id"] == "qrev_test"


@pytest.mark.asyncio
async def test_review_apply_200_and_409(client, db, admin_cookies, monkeypatch):
    project = await _project(db)
    row = db_models.ApiAiAnalysis(
        project_id=project.id, kind="review", status="ready",
        input_hash="x", result={"items": [{"target_id": 1}]},
    )
    db.add(row)
    await db.commit()

    async def fake_apply(*args, **kwargs):
        return {"applied": True}

    monkeypatch.setattr("app.routers.quality.review.apply_review_suggestion", fake_apply)
    resp = client.post(
        f"/api/quality/case-reviews/{row.id}/apply",
        json={"target_type": "draft", "target_id": 1},
        cookies=admin_cookies,
    )
    assert resp.status_code == 200, resp.text

    from app.services.quality_review import ReviewStale

    async def stale_apply(*args, **kwargs):
        raise ReviewStale("已变化")

    monkeypatch.setattr("app.routers.quality.review.apply_review_suggestion", stale_apply)
    resp = client.post(
        f"/api/quality/case-reviews/{row.id}/apply",
        json={"target_type": "draft", "target_id": 1},
        cookies=admin_cookies,
    )
    assert resp.status_code == 409, resp.text


@pytest.mark.asyncio
async def test_insights_200(client, db, admin_cookies, monkeypatch):
    project = await _project(db)
    row = db_models.ApiAiAnalysis(
        project_id=project.id, kind="cluster", status="ready",
        input_hash="x", result={"groups": [], "singletons": [], "flaky": []},
    )
    db.add(row)
    await db.commit()

    async def fake_insights(*args, **kwargs):
        return row, False

    monkeypatch.setattr("app.routers.quality.insights.generate_insights", fake_insights)
    resp = client.post(
        "/api/quality/insights", json={"project_id": project.id}, cookies=admin_cookies,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["kind"] == "cluster"


@pytest.mark.asyncio
async def test_defect_200(client, db, admin_cookies, monkeypatch):
    project = await _project(db)
    row = db_models.ApiAiAnalysis(
        project_id=project.id, kind="defect", status="ready",
        input_hash="x", result={"markdown": "# 缺陷", "evidence_files": []},
    )
    db.add(row)
    await db.commit()

    async def fake_defect(*args, **kwargs):
        return row, False

    monkeypatch.setattr("app.routers.quality.defect.create_defect_report", fake_defect)
    resp = client.post(
        "/api/quality/defect-reports",
        json={"project_id": project.id, "run_id": 1},
        cookies=admin_cookies,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["kind"] == "defect"


@pytest.mark.asyncio
async def test_defect_zip_404_without_evidence(client, db, admin_cookies):
    project = await _project(db)
    row = db_models.ApiAiAnalysis(
        project_id=project.id, kind="defect", status="ready",
        input_hash="x", result={"markdown": "# 缺陷", "evidence_files": ["reports/none.png"]},
    )
    db.add(row)
    await db.commit()
    resp = client.get(f"/api/quality/defect-reports/{row.id}/evidence.zip", cookies=admin_cookies)
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_failure_analysis_403_when_global_disabled(client, db, admin_cookies, monkeypatch):
    from types import SimpleNamespace

    project = await _project(db)
    _, run = await _run(db, project)
    monkeypatch.setattr(
        "app.routers.quality.common.get_settings",
        lambda: SimpleNamespace(quality_ai_enabled=False),
    )
    resp = client.post(
        "/api/quality/failure-analysis", json={"run_id": run.id}, cookies=admin_cookies,
    )
    assert resp.status_code == 403, resp.text
