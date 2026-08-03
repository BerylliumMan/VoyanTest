# tests/unit/test_execution_control.py
"""Unit tests for batch pause / resume / stop control plane."""
import asyncio

import pytest

from app import execution_control as ec


def _reset_control():
    ec._paused.clear()
    ec._stopped.clear()
    ec._resume_events.clear()
    ec._batch_tasks.clear()


@pytest.fixture(autouse=True)
def _clear_control():
    _reset_control()
    yield
    _reset_control()


@pytest.mark.asyncio
async def test_pause_resume_flags():
    bid = 101
    task = asyncio.create_task(asyncio.sleep(0))
    await ec.register_batch_task(bid, task)
    assert not ec.is_paused(bid)
    assert not ec.is_stopped(bid)

    assert await ec.request_pause(bid) is True
    assert ec.is_paused(bid)

    assert await ec.request_resume(bid) is True
    assert not ec.is_paused(bid)
    await task


@pytest.mark.asyncio
async def test_stop_clears_pause_and_blocks_resume():
    bid = 102
    task = asyncio.create_task(asyncio.sleep(0))
    await ec.register_batch_task(bid, task)
    await ec.request_pause(bid)
    await ec.request_stop(bid)
    assert ec.is_stopped(bid)
    assert not ec.is_paused(bid)
    assert await ec.request_resume(bid) is False
    assert await ec.request_pause(bid) is False
    await task


@pytest.mark.asyncio
async def test_wait_if_paused_unblocks_on_resume():
    bid = 103
    task = asyncio.create_task(asyncio.sleep(0))
    await ec.register_batch_task(bid, task)
    await ec.request_pause(bid)

    done = asyncio.Event()

    async def waiter():
        await ec.wait_if_paused(bid)
        done.set()

    w = asyncio.create_task(waiter())
    await asyncio.sleep(0.05)
    assert not done.is_set()
    await ec.request_resume(bid)
    await asyncio.wait_for(done.wait(), timeout=1.0)
    await w
    await task


@pytest.mark.asyncio
async def test_wait_if_paused_unblocks_on_stop():
    bid = 104
    task = asyncio.create_task(asyncio.sleep(0))
    await ec.register_batch_task(bid, task)
    await ec.request_pause(bid)

    async def waiter():
        await ec.wait_if_paused(bid)

    w = asyncio.create_task(waiter())
    await asyncio.sleep(0.05)
    await ec.request_stop(bid)
    await asyncio.wait_for(w, timeout=1.0)
    assert ec.is_stopped(bid)
    await task


@pytest.mark.asyncio
async def test_compute_preserves_paused_and_cancelled(db, sample_project, sample_testcase):
    from datetime import timedelta
    from app.crud.run import create_run_batch, _compute_batch_status
    from app import db_models
    from app.tz import now as tz_now

    batch = await create_run_batch(db, sample_project["id"], "Ctrl", total_cases=3)
    now = tz_now()
    run = db_models.TestRun(
        case_id=sample_testcase["id"],
        batch_id=batch.id,
        status="passed",
        start_time=now - timedelta(seconds=5),
        end_time=now,
        duration=5.0,
    )
    db.add(run)
    await db.commit()

    batch.status = "paused"
    await _compute_batch_status(db, batch, preloaded_runs=[run])
    assert batch.status == "paused"
    assert batch.passed == 1

    batch.status = "cancelled"
    await _compute_batch_status(db, batch, preloaded_runs=[run])
    assert batch.status == "cancelled"
