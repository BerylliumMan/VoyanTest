"""040 T010/T044：AI 端点契约（路由/权限/降级）。"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app import db_models
from app.services.api_ai import ApiAiUnavailable


@pytest.fixture(autouse=True)
def _mark_db_ready(db):
    """契约 conftest 用 TestAsyncSessionLocal 覆盖，但没有 is_ready 标记；
    这里包成 _LazySessionMaker，避免 auth 中间件在路由前返回 503。"""
    import app.database as db_mod
    previous = db_mod.AsyncSessionLocal
    wrapper = db_mod._LazySessionMaker()
    wrapper.configure(previous)
    db_mod.AsyncSessionLocal = wrapper
    yield
    db_mod.AsyncSessionLocal = previous


async def _project(db):
    project = db_models.Project(name="AI 契约项目")
    db.add(project)
    await db.flush()
    return project


async def _case_run(db, project, status="failed"):
    case = db_models.TestCase(
        project_id=project.id, project_case_number=0, name="AI 契约用例",
        case_kind="api", priority="medium", status="active",
    )
    db.add(case)
    await db.flush()
    run = db_models.TestRun(case_id=case.id, status=status)
    db.add(run)
    await db.commit()
    return case, run


@pytest.mark.asyncio
async def test_analyze_404_for_unknown_run(client, admin_cookies):
    resp = client.post(
        "/api/api-test/ai/analyze-failure", json={"run_id": -1},
        cookies=admin_cookies,
    )
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_analyze_400_for_passed_run(client, db, admin_cookies):
    project = await _project(db)
    _, run = await _case_run(db, project, status="passed")
    resp = client.post(
        "/api/api-test/ai/analyze-failure", json={"run_id": run.id},
        cookies=admin_cookies,
    )
    assert resp.status_code == 400, resp.text


@pytest.mark.asyncio
async def test_analyze_503_when_ai_unavailable(client, db, admin_cookies, monkeypatch):
    project = await _project(db)
    _, run = await _case_run(db, project)

    async def boom(*args, **kwargs):
        raise ApiAiUnavailable("模型超时")

    monkeypatch.setattr("app.routers.api_test.ai.analyze_failure", boom)
    resp = client.post(
        "/api/api-test/ai/analyze-failure", json={"run_id": run.id},
        cookies=admin_cookies,
    )
    assert resp.status_code == 503, resp.text
    assert "模型超时" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_analyze_403_when_project_ai_disabled(client, db, admin_cookies):
    project = await _project(db)
    project.ai_settings = {"api_ai_enabled": False}
    await db.commit()
    _, run = await _case_run(db, project)
    resp = client.post(
        "/api/api-test/ai/analyze-failure", json={"run_id": run.id},
        cookies=admin_cookies,
    )
    assert resp.status_code == 403, resp.text


@pytest.mark.asyncio
async def test_analyze_200_cached_contract(client, db, admin_cookies, monkeypatch):
    project = await _project(db)
    case, run = await _case_run(db, project)

    async def fake_analyze(*args, **kwargs):
        row = SimpleNamespace(
            id=1, project_id=project.id, run_id=run.id, case_id=case.id,
            kind="failure", status="ready", stale_at=None,
            result={"root_cause": "unknown", "summary": "证据不足"},
            model=None, created_at=None, updated_at=None,
        )
        return row, True

    monkeypatch.setattr("app.routers.api_test.ai.analyze_failure", fake_analyze)
    resp = client.post(
        "/api/api-test/ai/analyze-failure", json={"run_id": run.id},
        cookies=admin_cookies,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["cached"] is True
    assert body["result"]["root_cause"] == "unknown"


@pytest.mark.asyncio
async def test_contract_404_unknown_run(client, admin_cookies):
    resp = client.post(
        "/api/api-test/ai/check-contract", json={"run_id": -1},
        cookies=admin_cookies,
    )
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_contract_503_when_unavailable(client, db, admin_cookies, monkeypatch):
    project = await _project(db)
    _, run = await _case_run(db, project)

    async def boom(*args, **kwargs):
        raise ApiAiUnavailable("缺少响应证据")

    monkeypatch.setattr("app.services.api_ai_contract.check_contract", boom)
    resp = client.post(
        "/api/api-test/ai/check-contract", json={"run_id": run.id},
        cookies=admin_cookies,
    )
    assert resp.status_code == 503, resp.text
    assert "缺少响应证据" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_contract_200_structure(client, db, admin_cookies, monkeypatch):
    project = await _project(db)
    case, run = await _case_run(db, project)

    async def fake_contract(*args, **kwargs):
        return SimpleNamespace(
            id=2, project_id=project.id, run_id=run.id, case_id=case.id,
            kind="contract", status="ready", stale_at=None,
            result={"definition_id": 1, "status": "checked", "breaking": True,
                    "summary": "状态码变化", "findings": [{"kind": "status_changed"}]},
            model=None, created_at=None, updated_at=None,
        )

    monkeypatch.setattr("app.services.api_ai_contract.check_contract", fake_contract)
    resp = client.post(
        "/api/api-test/ai/check-contract", json={"run_id": run.id},
        cookies=admin_cookies,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["result"]["breaking"] is True
    assert body["result"]["findings"][0]["kind"] == "status_changed"
