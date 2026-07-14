"""Tests for app/scheduler.py — TaskScheduler and helpers."""
import asyncio
import pytest
from datetime import datetime
from unittest.mock import MagicMock, AsyncMock, patch

from app.scheduler import (
    ScheduledTask,
    TaskScheduler,
    CRON_EXAMPLES,
    validate_cron_expression,
    get_next_run_times,
    scheduler,
    start_scheduler,
    stop_scheduler,
)


class TestScheduledTask:
    @pytest.mark.asyncio
    async def test_create_defaults(self):
        t = ScheduledTask(
            id="t1", name="T", cron_expression="0 * * * *",
            task_type="testcase", target_id=1,
        )
        assert t.id == "t1"
        assert t.enabled is True
        assert t.run_count == 0
        assert t.fail_count == 0
        assert t.last_run is None
        assert t.created_at is not None

    @pytest.mark.asyncio
    async def test_calculate_next_run_valid(self):
        t = ScheduledTask(
            id="t1", name="T", cron_expression="0 * * * *",
            task_type="testcase", target_id=1,
        )
        result = t.calculate_next_run()
        assert result is not None
        assert isinstance(result, datetime)

    @pytest.mark.asyncio
    async def test_calculate_next_run_with_base(self):
        t = ScheduledTask(
            id="t1", name="T", cron_expression="0 * * * *",
            task_type="testcase", target_id=1,
        )
        base = datetime(2026, 1, 1, 0, 0, 0)
        result = t.calculate_next_run(base_time=base)
        assert result is not None

    @pytest.mark.asyncio
    async def test_calculate_next_run_invalid(self):
        t = ScheduledTask(
            id="t1", name="T", cron_expression="not a cron",
            task_type="testcase", target_id=1,
        )
        result = t.calculate_next_run()
        assert result is None


class TestTaskSchedulerBasic:
    @pytest.mark.asyncio
    async def test_init(self):
        s = TaskScheduler()
        assert s.tasks == {}
        assert s._running is False
        assert s._check_interval == 60
        assert s._executor is None

    @pytest.mark.asyncio
    async def test_set_executor(self):
        s = TaskScheduler()
        s.set_executor(lambda t: None)
        assert s._executor is not None

    @pytest.mark.asyncio
    async def test_add_task(self):
        s = TaskScheduler()
        t = await s.add_task("t1", "Task1", "0 * * * *", "testcase", 1)
        assert t.id == "t1"
        assert "t1" in s.tasks
        assert s.tasks["t1"].next_run is not None

    @pytest.mark.asyncio
    async def test_add_task_disabled(self):
        s = TaskScheduler()
        t = await s.add_task("t1", "Task1", "0 * * * *", "testcase", 1, enabled=False)
        assert t.enabled is False

    @pytest.mark.asyncio
    async def test_add_task_with_invalid_cron(self):
        s = TaskScheduler()
        t = await s.add_task("t1", "Task1", "not a cron", "testcase", 1)
        # Invalid cron: next_run stays None
        assert t.next_run is None

    @pytest.mark.asyncio
    async def test_remove_task(self):
        s = TaskScheduler()
        await s.add_task("t1", "Task1", "0 * * * *", "testcase", 1)
        assert await s.remove_task("t1") is True
        assert "t1" not in s.tasks

    @pytest.mark.asyncio
    async def test_remove_task_not_found(self):
        s = TaskScheduler()
        assert await s.remove_task("nonexistent") is False

    @pytest.mark.asyncio
    async def test_remove_task_cancels_handle(self):
        s = TaskScheduler()
        await s.add_task("t1", "Task1", "0 * * * *", "testcase", 1)
        mock_handle = MagicMock()
        s._task_handles["t1"] = mock_handle
        assert await s.remove_task("t1") is True
        mock_handle.cancel.assert_called_once()

    @pytest.mark.asyncio
    async def test_enable_task(self):
        s = TaskScheduler()
        await s.add_task("t1", "Task1", "0 * * * *", "testcase", 1, enabled=False)
        assert await s.enable_task("t1") is True
        assert s.tasks["t1"].enabled is True

    @pytest.mark.asyncio
    async def test_enable_task_not_found(self):
        s = TaskScheduler()
        assert await s.enable_task("nonexistent") is False

    @pytest.mark.asyncio
    async def test_disable_task(self):
        s = TaskScheduler()
        await s.add_task("t1", "Task1", "0 * * * *", "testcase", 1)
        assert await s.disable_task("t1") is True
        assert s.tasks["t1"].enabled is False

    @pytest.mark.asyncio
    async def test_disable_task_cancels_handle(self):
        s = TaskScheduler()
        await s.add_task("t1", "Task1", "0 * * * *", "testcase", 1)
        mock_handle = MagicMock()
        s._task_handles["t1"] = mock_handle
        assert await s.disable_task("t1") is True
        mock_handle.cancel.assert_called_once()

    @pytest.mark.asyncio
    async def test_disable_task_not_found(self):
        s = TaskScheduler()
        assert await s.disable_task("nonexistent") is False

    @pytest.mark.asyncio
    async def test_update_task_cron(self):
        s = TaskScheduler()
        await s.add_task("t1", "Task1", "0 * * * *", "testcase", 1)
        assert await s.update_task_cron("t1", "0 9 * * *") is True
        assert s.tasks["t1"].cron_expression == "0 9 * * *"

    @pytest.mark.asyncio
    async def test_update_task_cron_not_found(self):
        s = TaskScheduler()
        assert await s.update_task_cron("nonexistent", "0 * * * *") is False

    @pytest.mark.asyncio
    async def test_get_task(self):
        s = TaskScheduler()
        await s.add_task("t1", "Task1", "0 * * * *", "testcase", 1)
        t = await s.get_task("t1")
        assert t is not None
        assert t.id == "t1"

    @pytest.mark.asyncio
    async def test_get_task_not_found(self):
        s = TaskScheduler()
        assert await s.get_task("nonexistent") is None

    @pytest.mark.asyncio
    async def test_get_all_tasks(self):
        s = TaskScheduler()
        await s.add_task("t1", "Task1", "0 * * * *", "testcase", 1)
        await s.add_task("t2", "Task2", "0 9 * * *", "module", 2)
        tasks = await s.get_all_tasks()
        assert len(tasks) == 2

    @pytest.mark.asyncio
    async def test_get_enabled_tasks(self):
        s = TaskScheduler()
        await s.add_task("t1", "Task1", "0 * * * *", "testcase", 1, enabled=True)
        await s.add_task("t2", "Task2", "0 9 * * *", "module", 2, enabled=False)
        enabled = await s.get_enabled_tasks()
        assert len(enabled) == 1
        assert enabled[0].id == "t1"


class TestTaskSchedulerAsync:
    @pytest.mark.asyncio
    async def test_start_already_running(self):
        s = TaskScheduler()
        s._running = True
        await s.start()  # should return early

    @pytest.mark.asyncio
    async def test_start_creates_enabled_tasks(self):
        s = TaskScheduler()
        await s.add_task("t1", "Task1", "0 * * * *", "testcase", 1)

        with patch("app.scheduler.asyncio.create_task") as mock_create:
            async def stop_after():
                await asyncio.sleep(0.05)
                s._running = False

            s._check_interval = 0.01
            await asyncio.gather(s.start(), stop_after())
            assert mock_create.call_count >= 1

    @pytest.mark.asyncio
    async def test_stop_clears_handles(self):
        s = TaskScheduler()
        mock_handle = MagicMock()
        s._task_handles["t1"] = mock_handle
        await s.stop()
        assert s._running is False
        mock_handle.cancel.assert_called_once()
        assert s._task_handles == {}

    @pytest.mark.asyncio
    async def test_execute_task_no_executor(self):
        s = TaskScheduler()
        await s.add_task("t1", "Task1", "0 * * * *", "testcase", 1)
        task = await s.get_task("t1")
        await s._execute_task(task)
        # No executor → fail_count should remain 0
        assert task.fail_count == 0

    @pytest.mark.asyncio
    async def test_execute_task_with_executor(self):
        s = TaskScheduler()
        executor = AsyncMock()
        s.set_executor(executor)
        await s.add_task("t1", "Task1", "0 * * * *", "testcase", 1)
        task = await s.get_task("t1")
        await s._execute_task(task)
        executor.assert_awaited_once_with(task)
        assert task.fail_count == 0

    @pytest.mark.asyncio
    async def test_execute_task_with_failing_executor(self):
        s = TaskScheduler()
        executor = AsyncMock(side_effect=RuntimeError("boom"))
        s.set_executor(executor)
        await s.add_task("t1", "Task1", "0 * * * *", "testcase", 1)
        task = await s.get_task("t1")
        await s._execute_task(task)
        assert task.fail_count == 1

    @pytest.mark.asyncio
    async def test_run_task_now_existing(self):
        s = TaskScheduler()
        executor = AsyncMock()
        s.set_executor(executor)
        await s.add_task("t1", "Task1", "0 * * * *", "testcase", 1)
        with patch("app.scheduler.asyncio.create_task") as mock_create:
            assert await s.run_task_now("t1") is True
            mock_create.assert_called_once()

    @pytest.mark.asyncio
    async def test_run_task_now_not_found(self):
        s = TaskScheduler()
        assert await s.run_task_now("nonexistent") is False

    @pytest.mark.asyncio
    async def test_schedule_task_no_next_run_with_invalid_cron(self):
        s = TaskScheduler()
        await s.add_task("t1", "Task1", "not a cron", "testcase", 1)
        task = await s.get_task("t1")
        # task.next_run is None because cron is invalid
        assert task.next_run is None
        # Manually call _schedule_task - should break immediately
        await s._schedule_task(task)
        # Should have logged an error and broken out
        assert task.next_run is None

    @pytest.mark.asyncio
    async def test_schedule_task_executes_when_due(self):
        s = TaskScheduler()
        executor = AsyncMock()
        s.set_executor(executor)
        await s.add_task("t1", "Task1", "0 * * * *", "testcase", 1)
        task = await s.get_task("t1")
        from app.tz import now as tz_now
        task.next_run = tz_now()
        s._running = True

        async def stop_after():
            await asyncio.sleep(0.1)
            task.enabled = False

        try:
            await asyncio.wait_for(
                asyncio.gather(s._schedule_task(task), stop_after()),
                timeout=2.0
            )
        except asyncio.TimeoutError:
            task.enabled = False
        s._running = False
        assert task.run_count >= 1


class TestCronHelpers:
    @pytest.mark.asyncio
    async def test_validate_valid(self):
        assert validate_cron_expression("0 * * * *") is True
        assert validate_cron_expression("0 9 * * 1-5") is True

    @pytest.mark.asyncio
    async def test_validate_invalid(self):
        assert validate_cron_expression("not a cron") is False
        assert validate_cron_expression("") is False

    @pytest.mark.asyncio
    async def test_get_next_run_times_valid(self):
        result = get_next_run_times("0 * * * *", count=3)
        assert len(result) == 3
        assert all(isinstance(t, datetime) for t in result)

    @pytest.mark.asyncio
    async def test_get_next_run_times_invalid(self):
        result = get_next_run_times("not a cron", count=3)
        assert result == []

    @pytest.mark.asyncio
    async def test_cron_examples(self):
        assert "每小时" in CRON_EXAMPLES
        assert CRON_EXAMPLES["每小时"] == "0 * * * *"


class TestGlobalScheduler:
    @pytest.mark.asyncio
    async def test_global_scheduler_exists(self):
        from app.scheduler import scheduler
        assert isinstance(scheduler, TaskScheduler)

    @pytest.mark.asyncio
    async def test_start_stop_global(self):
        # Just exercise the helper functions - they call the global scheduler
        with patch.object(scheduler, "start", new_callable=AsyncMock) as mock_start:
            with patch.object(scheduler, "stop", new_callable=AsyncMock) as mock_stop:
                await start_scheduler()
                await stop_scheduler()
                mock_start.assert_awaited_once()
                mock_stop.assert_awaited_once()
