# tests/contract/test_report_api.py
"""报告 API 契约测试。"""
import pytest
from datetime import datetime, timezone
from app.tz import now as tz_now
from app import crud, db_models, models


async def _create_batch_with_runs(db, project_id, passed=0, failed=0):
    """辅助：创建批次并关联测试运行记录。"""
    batch = await crud.create_run_batch(db, project_id=project_id, total_cases=passed + failed)

    case = db_models.TestCase(
        project_id=project_id,
        name="统计测试用例",
        description="用于统计测试",
    )
    db.add(case)
    await db.flush()

    now = tz_now()
    for i in range(passed):
        run = db_models.TestRun(
            case_id=case.id,
            batch_id=batch.id,
            status="passed",
            start_time=now,
            end_time=now,
            duration=1.5,
        )
        db.add(run)
    for i in range(failed):
        run = db_models.TestRun(
            case_id=case.id,
            batch_id=batch.id,
            status="failed",
            start_time=now,
            end_time=now,
            duration=2.0,
        )
        db.add(run)

    await db.commit()
    await db.refresh(batch)
    await crud.update_batch_counters(db, batch.id, "passed")
    for _ in range(passed - 1):
        await crud.update_batch_counters(db, batch.id, "passed")
    for _ in range(failed):
        await crud.update_batch_counters(db, batch.id, "failed")
    return batch


async def _create_test_run(db, case_id, status="passed"):
    """辅助：创建测试运行记录。"""
    now = tz_now()
    return crud.create_test_run(
        db, case_id, status,
        start_time=now,
        end_time=now,
        duration=1.5,
    )


class TestStatistics:
    """GET /api/reports/statistics"""

    @pytest.mark.asyncio
    async def test_statistics_empty(self, client, admin_cookies):
        resp = client.get("/api/reports/statistics", cookies=admin_cookies)
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_runs"] == 0
        assert data["pass_rate"] == 0.0

    @pytest.mark.asyncio
    async def test_statistics_with_runs(self, client, admin_cookies, db, sample_project):
        pid = sample_project["id"]
        await _create_batch_with_runs(db, pid, passed=1, failed=1)
        resp = client.get("/api/reports/statistics", cookies=admin_cookies)
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_runs"] >= 2
        assert data["passed"] >= 1
        assert data["failed"] >= 1

    @pytest.mark.asyncio
    async def test_statistics_with_project_filter(self, client, admin_cookies, db, sample_project):
        pid = sample_project["id"]
        await _create_batch_with_runs(db, pid, passed=1, failed=0)
        resp = client.get(f"/api/reports/statistics?project_id={pid}", cookies=admin_cookies)
        assert resp.status_code == 200


class TestTrends:
    """GET /api/reports/trends"""

    @pytest.mark.asyncio
    async def test_trends_empty(self, client, admin_cookies):
        resp = client.get("/api/reports/trends", cookies=admin_cookies)
        assert resp.status_code == 200
        data = resp.json()
        assert data["period"] is not None
        assert isinstance(data["data"], list)

    @pytest.mark.asyncio
    async def test_trends_with_days_param(self, client, admin_cookies):
        resp = client.get("/api/reports/trends?days=7", cookies=admin_cookies)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["data"]) >= 7


class TestRunsList:
    """GET /api/reports/runs"""

    @pytest.mark.asyncio
    async def test_runs_list_empty(self, client, admin_cookies):
        resp = client.get("/api/reports/runs", cookies=admin_cookies)
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 0
        assert data["items"] == []

    @pytest.mark.asyncio
    async def test_runs_list_with_data(self, client, admin_cookies, db, sample_testcase):
        cid = sample_testcase["id"]
        await _create_test_run(db, cid, "passed")
        resp = client.get("/api/reports/runs", cookies=admin_cookies)
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] >= 1

    @pytest.mark.asyncio
    async def test_runs_list_with_status_filter(self, client, admin_cookies, db, sample_testcase):
        cid = sample_testcase["id"]
        await _create_test_run(db, cid, "passed")
        await _create_test_run(db, cid, "failed")
        resp = client.get("/api/reports/runs?status=passed", cookies=admin_cookies)
        assert resp.status_code == 200
        data = resp.json()
        for item in data["items"]:
            assert item["status"] == "passed"

    @pytest.mark.asyncio
    async def test_runs_list_pagination(self, client, admin_cookies, db, sample_testcase):
        cid = sample_testcase["id"]
        for i in range(5):
            await _create_test_run(db, cid, "passed")
        resp = client.get("/api/reports/runs?page=1&size=3", cookies=admin_cookies)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["items"]) == 3
        assert data["page"] == 1


class TestRunDetail:
    """GET /api/reports/runs/{id}"""

    @pytest.mark.asyncio
    async def test_run_detail_success(self, client, admin_cookies, db, sample_testcase):
        cid = sample_testcase["id"]
        run = await _create_test_run(db, cid, "passed")
        resp = client.get(f"/api/reports/runs/{run.id}", cookies=admin_cookies)
        assert resp.status_code == 200
        data = resp.json()
        assert data["run_id"] == run.id
        assert data["status"] == "passed"

    @pytest.mark.asyncio
    async def test_run_detail_not_found(self, client, admin_cookies):
        resp = client.get("/api/reports/runs/99999", cookies=admin_cookies)
        assert resp.status_code == 404


class TestSummary:
    """GET /api/reports/summary"""

    @pytest.mark.asyncio
    async def test_summary(self, client, admin_cookies):
        resp = client.get("/api/reports/summary", cookies=admin_cookies)
        assert resp.status_code == 200
        data = resp.json()
        assert "statistics" in data
        assert "trends" in data
        assert "recent_runs" in data


class TestBatchesList:
    """GET /api/reports/batches"""

    @pytest.mark.asyncio
    async def test_batches_empty(self, client, admin_cookies):
        resp = client.get("/api/reports/batches", cookies=admin_cookies)
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 0
        assert data["items"] == []

    @pytest.mark.asyncio
    async def test_batches_with_data(self, client, admin_cookies, db, sample_project):
        pid = sample_project["id"]
        await _create_batch_with_runs(db, pid, passed=2, failed=1)
        resp = client.get("/api/reports/batches", cookies=admin_cookies)
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] >= 1
        assert len(data["items"]) >= 1
        assert "name" in data["items"][0]
        assert "project_name" in data["items"][0]

    @pytest.mark.asyncio
    async def test_batches_pagination(self, client, admin_cookies, db, sample_project):
        pid = sample_project["id"]
        for _ in range(3):
            await _create_batch_with_runs(db, pid, passed=1, failed=0)
        resp = client.get("/api/reports/batches?page=1&size=2", cookies=admin_cookies)
        assert resp.status_code == 200
        data = resp.json()
        assert data["page"] == 1
        assert data["size"] == 2
        assert len(data["items"]) == 2
        assert data["total"] >= 3

    @pytest.mark.asyncio
    async def test_batches_with_project_filter(self, client, admin_cookies, db, sample_project):
        pid = sample_project["id"]
        await _create_batch_with_runs(db, pid, passed=1, failed=0)
        resp = client.get(f"/api/reports/batches?project_id={pid}", cookies=admin_cookies)
        assert resp.status_code == 200


class TestBatchDetail:
    """GET /api/reports/batches/{id}"""

    @pytest.mark.asyncio
    async def test_batch_detail_found(self, client, admin_cookies, db, sample_project):
        pid = sample_project["id"]
        batch = await _create_batch_with_runs(db, pid, passed=1, failed=1)
        resp = client.get(f"/api/reports/batches/{batch.id}", cookies=admin_cookies)
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == batch.id
        assert data["project_id"] == pid
        assert "runs" in data

    @pytest.mark.asyncio
    async def test_batch_detail_not_found(self, client, admin_cookies):
        resp = client.get("/api/reports/batches/99999", cookies=admin_cookies)
        assert resp.status_code == 404


class TestBatchUpdate:
    """PUT /api/reports/batches/{id}"""

    @pytest.mark.asyncio
    async def test_update_batch_name(self, client, admin_cookies, db, sample_project):
        pid = sample_project["id"]
        batch = await _create_batch_with_runs(db, pid, passed=1, failed=0)
        resp = client.put(f"/api/reports/batches/{batch.id}", json={
            "name": "重命名批次",
        }, cookies=admin_cookies)
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "重命名批次"

    @pytest.mark.asyncio
    async def test_update_batch_not_found(self, client, admin_cookies):
        resp = client.put("/api/reports/batches/99999", json={
            "name": "不存在",
        }, cookies=admin_cookies)
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_update_batch_empty_name(self, client, admin_cookies, db, sample_project):
        pid = sample_project["id"]
        batch = await _create_batch_with_runs(db, pid, passed=1, failed=0)
        resp = client.put(f"/api/reports/batches/{batch.id}", json={
            "name": "",
        }, cookies=admin_cookies)
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_update_batch_unauthenticated(self, client, db, sample_project):
        pid = sample_project["id"]
        batch = await _create_batch_with_runs(db, pid, passed=1, failed=0)
        client.cookies.clear()
        resp = client.put(f"/api/reports/batches/{batch.id}", json={"name": "x"})
        assert resp.status_code in (401, 307)


class TestBatchDelete:
    """DELETE /api/reports/batches/{id}"""

    @pytest.mark.asyncio
    async def test_delete_batch_found(self, client, admin_cookies, db, sample_project):
        pid = sample_project["id"]
        batch = await _create_batch_with_runs(db, pid, passed=1, failed=0)
        resp = client.delete(f"/api/reports/batches/{batch.id}", cookies=admin_cookies)
        assert resp.status_code == 200
        # 二次删除应返回 404
        resp2 = client.delete(f"/api/reports/batches/{batch.id}", cookies=admin_cookies)
        assert resp2.status_code == 404

    @pytest.mark.asyncio
    async def test_delete_batch_not_found(self, client, admin_cookies):
        resp = client.delete("/api/reports/batches/99999", cookies=admin_cookies)
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_delete_batch_unauthenticated(self, client, db, sample_project):
        pid = sample_project["id"]
        batch = await _create_batch_with_runs(db, pid, passed=1, failed=0)
        client.cookies.clear()
        resp = client.delete(f"/api/reports/batches/{batch.id}")
        assert resp.status_code in (401, 307)


class TestBatchExport:
    """GET /api/reports/batches/{id}/export"""

    @pytest.mark.asyncio
    async def test_export_batch_found(self, client, admin_cookies, db, sample_project):
        pid = sample_project["id"]
        batch = await _create_batch_with_runs(db, pid, passed=1, failed=0)
        resp = client.get(f"/api/reports/batches/{batch.id}/export", cookies=admin_cookies)
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == batch.id
        assert data["project_id"] == pid
        assert "runs" in data

    @pytest.mark.asyncio
    async def test_export_batch_not_found(self, client, admin_cookies):
        resp = client.get("/api/reports/batches/99999/export", cookies=admin_cookies)
        assert resp.status_code == 404


class TestCompareBatches:
    """POST /api/reports/compare"""

    @pytest.mark.asyncio
    async def test_compare_two_batches(self, client, admin_cookies, db, sample_project):
        pid = sample_project["id"]
        batch_a = await _create_batch_with_runs(db, pid, passed=2, failed=1)
        batch_b = await _create_batch_with_runs(db, pid, passed=3, failed=0)
        resp = client.post(
            f"/api/reports/compare?batch_a={batch_a.id}&batch_b={batch_b.id}",
            cookies=admin_cookies,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "a" in data
        assert "b" in data
        assert "passed_diff" in data
        assert "failed_diff" in data
        a = data["a"]
        assert a["passed"] == 2
        assert a["failed"] == 1
        b = data["b"]
        assert b["passed"] == 3
        assert b["failed"] == 0

    @pytest.mark.asyncio
    async def test_compare_batch_not_found(self, client, admin_cookies):
        resp = client.post(
            "/api/reports/compare?batch_a=99999&batch_b=88888",
            cookies=admin_cookies,
        )
        assert resp.status_code == 404


class TestRunsByBatch:
    """GET /api/reports/runs — filtered by batch_id"""

    @pytest.mark.asyncio
    async def test_runs_by_batch(self, client, admin_cookies, db, sample_project, sample_testcase):
        pid = sample_project["id"]
        batch = await _create_batch_with_runs(db, pid, passed=2, failed=1)
        resp = client.get(f"/api/reports/runs?batch_id={batch.id}", cookies=admin_cookies)
        assert resp.status_code == 200
        data = resp.json()
        assert "items" in data

    @pytest.mark.asyncio
    async def test_runs_without_batch_filter(self, client, admin_cookies):
        resp = client.get("/api/reports/runs", cookies=admin_cookies)
        assert resp.status_code == 200
        data = resp.json()
        assert "items" in data


class TestBatchControl:
    """POST /api/reports/batches/{id}/pause|resume|stop"""

    @pytest.mark.asyncio
    async def test_pause_resume_stop_happy_path(self, client, admin_cookies, db, sample_project):
        from app import execution_control
        import asyncio

        pid = sample_project["id"]
        batch = await crud.create_run_batch(db, project_id=pid, total_cases=2)
        batch.status = "running"
        await db.commit()

        task = asyncio.create_task(asyncio.sleep(60))
        await execution_control.register_batch_task(batch.id, task)
        try:
            resp = client.post(f"/api/reports/batches/{batch.id}/pause", cookies=admin_cookies)
            assert resp.status_code == 200
            assert resp.json()["status"] == "paused"
            await db.refresh(batch)
            assert batch.status == "paused"

            resp = client.post(f"/api/reports/batches/{batch.id}/resume", cookies=admin_cookies)
            assert resp.status_code == 200
            assert resp.json()["status"] == "running"

            resp = client.post(f"/api/reports/batches/{batch.id}/stop", cookies=admin_cookies)
            assert resp.status_code == 200
            assert resp.json()["status"] == "cancelled"
            await db.refresh(batch)
            assert batch.status == "cancelled"
            assert batch.finished_at is not None
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            await execution_control.clear_batch(batch.id)

    @pytest.mark.asyncio
    async def test_pause_wrong_status_409(self, client, admin_cookies, db, sample_project):
        pid = sample_project["id"]
        batch = await _create_batch_with_runs(db, pid, passed=1, failed=0)
        # finished batch cannot pause
        resp = client.post(f"/api/reports/batches/{batch.id}/pause", cookies=admin_cookies)
        assert resp.status_code == 409

    @pytest.mark.asyncio
    async def test_resume_wrong_status_409(self, client, admin_cookies, db, sample_project):
        pid = sample_project["id"]
        batch = await crud.create_run_batch(db, project_id=pid, total_cases=1)
        batch.status = "running"
        await db.commit()
        resp = client.post(f"/api/reports/batches/{batch.id}/resume", cookies=admin_cookies)
        assert resp.status_code == 409

    @pytest.mark.asyncio
    async def test_stop_not_found(self, client, admin_cookies):
        resp = client.post("/api/reports/batches/99999/stop", cookies=admin_cookies)
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_stop_cancels_pending_runs(self, client, admin_cookies, db, sample_project, sample_testcase):
        from app import execution_control
        import asyncio

        pid = sample_project["id"]
        batch = await crud.create_run_batch(db, project_id=pid, total_cases=2)
        batch.status = "running"
        now = tz_now()
        pending = db_models.TestRun(
            case_id=sample_testcase["id"],
            batch_id=batch.id,
            status="pending",
            start_time=None,
            end_time=None,
        )
        db.add(pending)
        await db.commit()

        task = asyncio.create_task(asyncio.sleep(60))
        await execution_control.register_batch_task(batch.id, task)
        try:
            resp = client.post(f"/api/reports/batches/{batch.id}/stop", cookies=admin_cookies)
            assert resp.status_code == 200
            await db.refresh(pending)
            assert pending.status == "cancelled"
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            await execution_control.clear_batch(batch.id)
