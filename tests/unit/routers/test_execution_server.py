"""execution/_server.py 路由测试 — 服务端执行端点。

mock BackgroundTasks 避免实际执行 runner 函数。
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

from fastapi import BackgroundTasks


pytestmark = pytest.mark.asyncio


class TestRunCase:
    """POST /api/testcases/{case_id}/run"""

    async def test_case_not_found_returns_404(self, client, admin_cookies):
        resp = client.post("/api/testcases/99999/run", cookies=admin_cookies)
        assert resp.status_code == 404

    async def test_success_returns_batch_id(self, client, admin_cookies, sample_testcase):
        mock_bt = MagicMock(spec=BackgroundTasks)
        with patch.object(mock_bt, 'add_task'):
            resp = client.post(
                f"/api/testcases/{sample_testcase['id']}/run",
                cookies=admin_cookies,
            )
        assert resp.status_code == 200
        data = resp.json()
        assert "batch_id" in data
        assert data["batch_id"] > 0
        assert data["status"] == "running"


class TestRunDebug:
    """POST /api/testcases/{case_id}/run-debug"""

    async def test_case_not_found_returns_404(self, client, admin_cookies):
        resp = client.post("/api/testcases/99999/run-debug", cookies=admin_cookies)
        assert resp.status_code == 404

    async def test_success_returns_run_id(self, client, admin_cookies, sample_testcase):
        with patch("app.routers.testcase.execution._server._run_debug_mode", new_callable=AsyncMock):
            resp = client.post(
                f"/api/testcases/{sample_testcase['id']}/run-debug",
                cookies=admin_cookies,
            )
        assert resp.status_code == 200
        data = resp.json()
        assert "run_id" in data
        assert "batch_id" in data
        assert data["status"] == "debug_running"


class TestBatchRun:
    """POST /api/testcases/batch-run"""

    async def test_empty_returns_400(self, client, admin_cookies):
        resp = client.post(
            "/api/testcases/batch-run",
            json={"case_ids": []},
            cookies=admin_cookies,
        )
        assert resp.status_code == 400

    @pytest.mark.xfail(reason="fixture session污染，单独跑通过")
    async def test_success(self, client, admin_cookies, sample_testcase):
        resp = client.post(
            "/api/testcases/batch-run",
            json={"case_ids": [sample_testcase["id"]]},
            cookies=admin_cookies,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "running"
        assert data["total"] >= 1


class TestRunModule:
    """POST /api/testcases/module/{module_id}/run"""

    async def test_module_not_found_returns_404(self, client, admin_cookies):
        resp = client.post("/api/testcases/module/99999/run", cookies=admin_cookies)
        assert resp.status_code == 404

    @pytest.mark.xfail(reason="fixture session污染，单独跑通过")
    async def test_success(self, client, admin_cookies, sample_testcase, db):
        from app import db_models
        module = db_models.Module(
            project_id=sample_testcase["project_id"], name="测试模块",
        )
        db.add(module)
        await db.commit()
        await db.refresh(module)

        tc = db_models.TestCase(
            project_id=sample_testcase["project_id"], module_id=module.id,
            name="模块内用例",
        )
        db.add(tc)
        await db.commit()

        resp = client.post(
            f"/api/testcases/module/{module.id}/run",
            cookies=admin_cookies,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "batch_id" in data


class TestRunProject:
    """POST /api/testcases/project/{project_id}/run"""

    async def test_project_not_found_returns_404(self, client, admin_cookies):
        resp = client.post("/api/testcases/project/99999/run", cookies=admin_cookies)
        assert resp.status_code == 404

    async def test_success(self, client, admin_cookies, sample_project, sample_testcase):
        resp = client.post(
            f"/api/testcases/project/{sample_project['id']}/run",
            cookies=admin_cookies,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "batch_id" in data
