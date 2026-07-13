"""execution/_client.py 路由测试 — Agent 执行端点。

mock agent_manager 避免实际 Agent 连接。
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app import crud


pytestmark = pytest.mark.asyncio


class TestRunClient:
    """POST /api/testcases/{case_id}/run-client"""

    async def test_case_not_found_returns_404(self, client, admin_cookies):
        resp = client.post("/api/testcases/99999/run-client", cookies=admin_cookies)
        assert resp.status_code == 404

    async def test_no_agent_returns_400(self, client, admin_cookies, sample_testcase):
        with patch("agent.manager.agent_manager.get_online_agents", return_value=[]):
            resp = client.post(
                f"/api/testcases/{sample_testcase['id']}/run-client",
                cookies=admin_cookies,
            )
        assert resp.status_code == 400
        assert "No client agents" in resp.json()["detail"]

    async def test_agent_not_found_returns_400(self, client, admin_cookies, sample_testcase):
        with patch("agent.manager.agent_manager.get_online_agents", return_value=[
            MagicMock(id=1, name="real-agent")
        ]):
            resp = client.post(
                f"/api/testcases/{sample_testcase['id']}/run-client?agent_name=nonexistent",
                cookies=admin_cookies,
            )
        assert resp.status_code == 400
        assert "not found" in resp.json()["detail"].lower()

    async def test_no_steps_returns_400(self, client, admin_cookies, sample_testcase, db):
        # Create a test case with no steps
        from app import db_models
        from app.models import TestStepCreatePayload
        tc_no_steps = db_models.TestCase(
            project_id=sample_testcase["project_id"], name="无步骤用例"
        )
        db.add(tc_no_steps)
        await db.commit()
        # Don't add any steps

        with patch("agent.manager.agent_manager.get_online_agents", return_value=[
            MagicMock(id=1, name="test-agent")
        ]):
            resp = client.post(
                f"/api/testcases/{tc_no_steps.id}/run-client",
                cookies=admin_cookies,
            )
        assert resp.status_code == 400
        assert "no steps" in resp.json()["detail"].lower()

    async def test_success_returns_batch_id(self, client, admin_cookies, sample_testcase):
        mock_agent = MagicMock(id=1, name="test-agent")
        mock_agent_manager = MagicMock()
        mock_agent_manager.get_online_agents = AsyncMock(return_value=[mock_agent])
        mock_agent_manager.execute_on_agent = AsyncMock(return_value=[{"success": True}])

        with patch("agent.manager.agent_manager", mock_agent_manager):
            resp = client.post(
                f"/api/testcases/{sample_testcase['id']}/run-client",
                cookies=admin_cookies,
            )
        assert resp.status_code == 200
        data = resp.json()
        assert "batch_id" in data
        assert data["batch_id"] > 0
        assert "run_id" in data


class TestBatchRunClient:
    """POST /api/testcases/batch-run-client"""

    async def test_empty_case_ids_returns_400(self, client, admin_cookies):
        resp = client.post(
            "/api/testcases/batch-run-client",
            json={"case_ids": []},
            cookies=admin_cookies,
        )
        assert resp.status_code == 400

    async def test_no_agent_returns_400(self, client, admin_cookies, sample_testcase):
        with patch("agent.manager.agent_manager.get_online_agents", return_value=[]):
            resp = client.post(
                "/api/testcases/batch-run-client",
                json={"case_ids": [sample_testcase["id"]]},
                cookies=admin_cookies,
            )
        assert resp.status_code == 400
