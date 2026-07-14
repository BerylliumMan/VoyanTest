"""Tests for app/crud/run.py — TestRun and RunBatch CRUD operations."""
from datetime import datetime, timedelta
import pytest
from sqlalchemy import select
from app import db_models
from app.tz import now as tz_now
from app.crud.run import (
    create_test_run, update_test_run_status, create_run_log,
    create_run_batch, get_run_batch, list_run_batches,
    update_run_batch, delete_run_batch, update_batch_counters,
)


class TestTestRun:
    @pytest.mark.asyncio
    async def test_create_test_run(self, db, sample_testcase):
        now = tz_now()
        run = await create_test_run(db, sample_testcase["id"], "running", now, now)
        assert run.id is not None
        assert run.status == "running"
        assert run.case_id == sample_testcase["id"]

    @pytest.mark.asyncio
    async def test_update_status(self, db, sample_testcase):
        now = tz_now()
        run = await create_test_run(db, sample_testcase["id"], "running", now, now)
        updated = await update_test_run_status(db, run.id, "passed", end_time=now, duration=10.5)
        assert updated is not None
        assert updated.status == "passed"
        assert updated.duration == 10.5

    @pytest.mark.asyncio
    async def test_update_status_not_found(self, db):
        assert await update_test_run_status(db, 99999, "passed") is None

    @pytest.mark.asyncio
    async def test_create_run_log(self, db, sample_testcase):
        now = tz_now()
        run = await create_test_run(db, sample_testcase["id"], "running", now, now)
        log = await create_run_log(db, run.id, "INFO", "test message", step_id=1)
        assert log.id is not None
        assert log.level == "INFO"

    @pytest.mark.asyncio
    async def test_create_run_log_with_screenshot(self, db, sample_testcase):
        now = tz_now()
        run = await create_test_run(db, sample_testcase["id"], "running", now, now)
        log = await create_run_log(db, run.id, "ERROR", "failed", step_id=1, screenshot_path="/ss/1.png")
        assert log.screenshot_path == "/ss/1.png"


class TestRunBatch:
    @pytest.mark.asyncio
    async def test_create_batch(self, db, sample_project):
        batch = await create_run_batch(db, sample_project["id"], "Test Batch", total_cases=5)
        assert batch.id is not None
        assert batch.status == "running"
        assert batch.total_cases == 5

    @pytest.mark.asyncio
    async def test_get_batch_found(self, db, sample_project):
        batch = await create_run_batch(db, sample_project["id"], "Get Test", total_cases=1)
        found = await get_run_batch(db, batch.id)
        assert found is not None
        assert found.id == batch.id

    @pytest.mark.asyncio
    async def test_get_batch_not_found(self, db):
        assert await get_run_batch(db, 99999) is None

    @pytest.mark.asyncio
    async def test_list_batches_empty(self, db):
        result = await list_run_batches(db)
        assert result["total"] >= 0
        assert "items" in result

    @pytest.mark.asyncio
    async def test_list_batches_with_data(self, db, sample_project):
        await create_run_batch(db, sample_project["id"], "Batch 1", total_cases=2)
        result = await list_run_batches(db)
        assert result["total"] >= 1

    @pytest.mark.asyncio
    async def test_list_batches_filter_by_project(self, db, sample_project):
        await create_run_batch(db, sample_project["id"], "Batch P", total_cases=1)
        result = await list_run_batches(db, project_id=sample_project["id"])
        assert result["total"] >= 1

    @pytest.mark.asyncio
    async def test_list_batches_filter_by_status(self, db, sample_project):
        await create_run_batch(db, sample_project["id"], "Batch S", total_cases=1)
        result = await list_run_batches(db, status="running")
        assert result["total"] >= 1

    @pytest.mark.asyncio
    async def test_list_batches_pagination(self, db, sample_project):
        await create_run_batch(db, sample_project["id"], "Page Batch", total_cases=1)
        result = await list_run_batches(db, page=1, size=10)
        assert result["page"] == 1
        assert result["size"] == 10

    @pytest.mark.asyncio
    async def test_list_batches_preloads_runs(self, db, sample_project, sample_testcase):
        """覆盖 list_run_batches 预加载 runs 的内层循环。"""
        batch = await create_run_batch(db, sample_project["id"], "With Runs", total_cases=1)
        now = tz_now()
        run = await create_test_run(db, sample_testcase["id"], "passed", now, now)
        run.batch_id = batch.id
        await db.commit()

        result = await list_run_batches(db, project_id=sample_project["id"])
        # expire_all 在 AsyncSession 上触发 MissingGreenlet，改用 commit
        await db.commit()

        assert result["total"] >= 1
        assert result["items"][0].id == batch.id

    @pytest.mark.asyncio
    async def test_update_batch_found(self, db, sample_project):
        batch = await create_run_batch(db, sample_project["id"], "Old Name", total_cases=1)
        updated = await update_run_batch(db, batch.id, name="New Name")
        assert updated is not None
        assert updated.name == "New Name"

    @pytest.mark.asyncio
    async def test_update_batch_not_found(self, db):
        assert await update_run_batch(db, 99999, name="Nope") is None

    @pytest.mark.asyncio
    async def test_delete_batch_found(self, db, sample_project):
        batch = await create_run_batch(db, sample_project["id"], "Delete Me", total_cases=1)
        assert await delete_run_batch(db, batch.id) is True
        assert await get_run_batch(db, batch.id) is None

    @pytest.mark.asyncio
    async def test_delete_batch_not_found(self, db):
        assert await delete_run_batch(db, 99999) is False


class TestBatchCounters:
    @pytest.mark.asyncio
    async def test_update_counter_passed(self, db, sample_project):
        batch = await create_run_batch(db, sample_project["id"], "Counter", total_cases=2)
        result = await update_batch_counters(db, batch.id, "passed")
        assert result is not None
        assert result.passed == 1

    @pytest.mark.asyncio
    async def test_update_counter_failed(self, db, sample_project):
        batch = await create_run_batch(db, sample_project["id"], "Counter F", total_cases=2)
        result = await update_batch_counters(db, batch.id, "failed")
        assert result is not None
        assert result.failed == 1

    @pytest.mark.asyncio
    async def test_update_counter_not_found(self, db):
        assert await update_batch_counters(db, 99999, "passed") is None

    @pytest.mark.asyncio
    async def test_update_counter_completes_batch(self, db, sample_project):
        batch = await create_run_batch(db, sample_project["id"], "Complete", total_cases=1)
        await update_batch_counters(db, batch.id, "passed")
        refreshed = await get_run_batch(db, batch.id)
        assert refreshed.finished_at is not None


class TestBatchStatus:
    @pytest.mark.asyncio
    async def test_compute_no_runs_old(self, db, sample_project):
        from app.crud.run import _compute_batch_status
        batch = await create_run_batch(db, sample_project["id"], "Old Batch", total_cases=1)
        batch.created_at = tz_now() - timedelta(seconds=60)
        await _compute_batch_status(db, batch, preloaded_runs=[])
        assert batch.status == "failed"
        db.expire_all()

    @pytest.mark.asyncio
    async def test_compute_no_runs_recent(self, db, sample_project):
        from app.crud.run import _compute_batch_status
        batch = await create_run_batch(db, sample_project["id"], "Recent Batch", total_cases=1)
        await _compute_batch_status(db, batch, preloaded_runs=[])
        assert batch.status in ("running", "failed")
        db.expire_all()


class TestTestRunReportPath:

    @pytest.mark.asyncio
    async def test_update_status_sets_report_path(self, db, sample_testcase):
        now = tz_now()
        run = await create_test_run(db, sample_testcase["id"], "running", now, now)
        updated = await update_test_run_status(
            db, run.id, "passed", end_time=now, duration=5.0,
            report_path="/reports/run-{}.html".format(run.id),
        )
        assert updated is not None
        assert updated.report_path == "/reports/run-{}.html".format(run.id)
        assert updated.status == "passed"
        assert updated.duration == 5.0

    @pytest.mark.asyncio
    async def test_update_status_empty_report_path_keeps_existing(self, db, sample_testcase):
        now = tz_now()
        run = await create_test_run(db, sample_testcase["id"], "running", now, now)
        await update_test_run_status(db, run.id, "running", report_path="/old.html")
        updated = await update_test_run_status(db, run.id, "passed", report_path="")
        assert updated.report_path == "/old.html"


class TestDeleteRunBatchCascade:

    @pytest.mark.asyncio
    async def test_delete_batch_cascades_runs_and_logs(self, db, sample_project, sample_testcase):
        batch = await create_run_batch(db, sample_project["id"], "Cascade", total_cases=1)
        now = tz_now()
        run_a = await create_test_run(db, sample_testcase["id"], "passed", now, now)
        run_b = await create_test_run(db, sample_testcase["id"], "failed", now, now)
        run_a.batch_id = batch.id
        run_b.batch_id = batch.id
        await db.commit()

        log_a = await create_run_log(db, run_a.id, "INFO", "log a")
        log_b = await create_run_log(db, run_b.id, "ERROR", "log b")

        run_a_id, run_b_id, log_a_id, log_b_id = run_a.id, run_b.id, log_a.id, log_b.id
        batch_id = batch.id

        assert await delete_run_batch(db, batch_id) is True

        assert await get_run_batch(db, batch_id) is None
        run_a_check = (await db.execute(select(db_models.TestRun).where(db_models.TestRun.id == run_a_id))).scalar_one_or_none()
        assert run_a_check is None
        run_b_check = (await db.execute(select(db_models.TestRun).where(db_models.TestRun.id == run_b_id))).scalar_one_or_none()
        assert run_b_check is None
        log_a_check = (await db.execute(select(db_models.RunLog).where(db_models.RunLog.id == log_a_id))).scalar_one_or_none()
        assert log_a_check is None
        log_b_check = (await db.execute(select(db_models.RunLog).where(db_models.RunLog.id == log_b_id))).scalar_one_or_none()
        assert log_b_check is None

    @pytest.mark.asyncio
    async def test_delete_batch_with_runs_no_logs(self, db, sample_project, sample_testcase):
        batch = await create_run_batch(db, sample_project["id"], "Cascade NoLogs", total_cases=1)
        now = tz_now()
        run = await create_test_run(db, sample_testcase["id"], "running", now, now)
        run.batch_id = batch.id
        await db.commit()

        assert await delete_run_batch(db, batch.id) is True
        run_check = (await db.execute(select(db_models.TestRun).where(db_models.TestRun.id == run.id))).scalar_one_or_none()
        assert run_check is None


class TestComputeBatchStatusAdvanced:

    @pytest.mark.asyncio
    async def _make_run(self, db, case_id, batch_id, status, start_time=None, end_time=None, duration=None):
        run = db_models.TestRun(
            case_id=case_id,
            batch_id=batch_id,
            status=status,
            start_time=start_time,
            end_time=end_time,
            duration=duration,
        )
        db.add(run)
        await db.commit()
        await db.refresh(run)
        return run

    @pytest.mark.asyncio
    async def test_compute_stuck_pending_marked_failed(self, db, sample_project, sample_testcase):
        from app.crud.run import _compute_batch_status
        batch = await create_run_batch(db, sample_project["id"], "Stuck Pending", total_cases=1)
        stuck_start = tz_now() - timedelta(seconds=60)
        run = await self._make_run(db, sample_testcase["id"], batch.id, "pending", start_time=stuck_start)

        await _compute_batch_status(db, batch, preloaded_runs=[run])

        assert run.status == "failed"
        assert run.end_time is not None
        assert run.duration == 0.0
        assert batch.status == "failed"
        assert batch.failed == 1
        assert batch.passed == 0

        db.expire_all()

    @pytest.mark.asyncio
    async def test_compute_stuck_pending_with_naive_datetime(self, db, sample_project, sample_testcase):
        from app.crud.run import _compute_batch_status
        batch = await create_run_batch(db, sample_project["id"], "Stuck Naive", total_cases=1)
        naive_start = (tz_now() - timedelta(seconds=60)).replace(tzinfo=None)
        run = await self._make_run(db, sample_testcase["id"], batch.id, "pending", start_time=naive_start)

        await _compute_batch_status(db, batch, preloaded_runs=[run])

        assert run.status == "failed"
        assert batch.status == "failed"

        db.expire_all()

    @pytest.mark.asyncio
    async def test_compute_running_state(self, db, sample_project, sample_testcase):
        from app.crud.run import _compute_batch_status
        batch = await create_run_batch(db, sample_project["id"], "Running", total_cases=3)
        now = tz_now()
        run_running = await self._make_run(db, sample_testcase["id"], batch.id, "running", start_time=now)
        run_passed = await self._make_run(db, sample_testcase["id"], batch.id, "passed",
                                     start_time=now - timedelta(seconds=10),
                                     end_time=now, duration=10.0)

        await _compute_batch_status(db, batch, preloaded_runs=[run_running, run_passed])

        assert batch.status == "running"
        assert batch.passed == 1
        assert batch.failed == 0

        db.expire_all()

    @pytest.mark.asyncio
    async def test_compute_pending_counts_as_running(self, db, sample_project, sample_testcase):
        from app.crud.run import _compute_batch_status
        batch = await create_run_batch(db, sample_project["id"], "Has Pending", total_cases=2)
        run = await self._make_run(db, sample_testcase["id"], batch.id, "pending", start_time=None)

        await _compute_batch_status(db, batch, preloaded_runs=[run])

        assert batch.status == "running"

        db.expire_all()

    @pytest.mark.asyncio
    async def test_compute_partial_state(self, db, sample_project, sample_testcase):
        from app.crud.run import _compute_batch_status
        batch = await create_run_batch(db, sample_project["id"], "Partial", total_cases=3)
        now = tz_now()
        run_passed = await self._make_run(db, sample_testcase["id"], batch.id, "passed",
                                     start_time=now - timedelta(seconds=5),
                                     end_time=now, duration=5.0)

        await _compute_batch_status(db, batch, preloaded_runs=[run_passed])

        assert batch.status == "partial"
        assert batch.passed == 1
        assert batch.failed == 0

        db.expire_all()

    @pytest.mark.asyncio
    async def test_compute_all_passed(self, db, sample_project, sample_testcase):
        from app.crud.run import _compute_batch_status
        batch = await create_run_batch(db, sample_project["id"], "All Pass", total_cases=2)
        now = tz_now()
        r1 = await self._make_run(db, sample_testcase["id"], batch.id, "passed",
                            start_time=now - timedelta(seconds=10), end_time=now, duration=10.0)
        r2 = await self._make_run(db, sample_testcase["id"], batch.id, "passed",
                            start_time=now - timedelta(seconds=5), end_time=now, duration=5.0)

        await _compute_batch_status(db, batch, preloaded_runs=[r1, r2])

        assert batch.status == "passed"
        assert batch.passed == 2
        assert batch.failed == 0

        db.expire_all()

    @pytest.mark.asyncio
    async def test_compute_mixed_passed_failed_completed(self, db, sample_project, sample_testcase):
        from app.crud.run import _compute_batch_status
        batch = await create_run_batch(db, sample_project["id"], "Mixed", total_cases=2)
        now = tz_now()
        r_pass = await self._make_run(db, sample_testcase["id"], batch.id, "passed",
                                start_time=now - timedelta(seconds=10), end_time=now, duration=10.0)
        r_fail = await self._make_run(db, sample_testcase["id"], batch.id, "failed",
                                start_time=now - timedelta(seconds=8), end_time=now, duration=8.0)

        await _compute_batch_status(db, batch, preloaded_runs=[r_pass, r_fail])

        assert batch.status == "failed"
        assert batch.passed == 1
        assert batch.failed == 1

        db.expire_all()

    @pytest.mark.asyncio
    async def test_compute_stuck_pending_preserves_real_failed_count(self, db, sample_project, sample_testcase):
        from app.crud.run import _compute_batch_status
        batch = await create_run_batch(db, sample_project["id"], "Stuck Mixed", total_cases=2)
        now = tz_now()
        real_failed = await self._make_run(db, sample_testcase["id"], batch.id, "failed",
                                      start_time=now - timedelta(seconds=5), end_time=now, duration=5.0)
        stuck = await self._make_run(db, sample_testcase["id"], batch.id, "pending",
                               start_time=now - timedelta(seconds=60))

        await _compute_batch_status(db, batch, preloaded_runs=[real_failed, stuck])

        assert stuck.status == "failed"
        assert batch.failed == 2
        assert batch.passed == 0
        assert batch.status == "failed"

        db.expire_all()
