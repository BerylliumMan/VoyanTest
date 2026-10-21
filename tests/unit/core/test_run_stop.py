"""038 US3：批次停止（观察器 / run 取消 / 状态保护）。"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


class _FakeDB:
    def __init__(self, status):
        self.status = status
        self.queries = 0

    async def execute(self, stmt, *a, **kw):
        self.queries += 1
        res = MagicMock()
        res.scalar_one_or_none.return_value = self.status
        return res


@pytest.mark.asyncio
async def test_watcher_memory_stop(execution_control_state):
    from core.run_control import BatchStopWatcher

    ec = execution_control_state
    ec._stopped.add(42)
    w = BatchStopWatcher(_FakeDB("running"), 42)
    assert await w.should_stop() is True


@pytest.mark.asyncio
async def test_watcher_db_status(execution_control_state):
    from core.run_control import BatchStopWatcher

    db = _FakeDB("cancelled")
    w = BatchStopWatcher(db, 77, poll_ms=100)
    assert await w.should_stop() is True
    assert db.queries == 1


@pytest.mark.asyncio
async def test_watcher_no_batch_never_stops(execution_control_state):
    from core.run_control import BatchStopWatcher

    w = BatchStopWatcher(_FakeDB("cancelled"), None)
    assert await w.should_stop() is False


def test_effective_run_status_preserves_cancelled():
    from core.runner._persistence import effective_run_status

    assert effective_run_status("cancelled", "passed") == "cancelled"
    assert effective_run_status("cancelled", "failed") == "cancelled"
    assert effective_run_status("running", "passed") == "passed"
    assert effective_run_status("cancelled", "cancelled") == "cancelled"


@pytest.mark.asyncio
async def test_bridge_cancel_marks_run_and_test_runs(monkeypatch):
    from core.agent_bridge import AgentBridge
    import core.agent_bridge as ab

    bridge = AgentBridge.__new__(AgentBridge)
    bridge.db = AsyncMock()
    bridge._batch_id = 5
    bridge._synced_test_run_ids = []

    res = MagicMock()
    res.fetchall.return_value = [(11,)]
    bridge.db.execute = AsyncMock(return_value=res)

    called = {}

    async def _fake_update(db, run_id, status, **kw):
        called["run_id"] = run_id
        called["status"] = status
        called["kw"] = kw

    monkeypatch.setattr(ab.crud_agent_run, "update_agent_run_status", _fake_update)

    run = SimpleNamespace(id=7, case_id=9)
    await AgentBridge._cancel(bridge, run, "已停止（用户取消）")

    assert called["run_id"] == 7
    assert called["status"] == "cancelled"
    assert called["kw"].get("error") == "已停止（用户取消）"
    sql = "\n".join(str(c.args[0]) for c in bridge.db.execute.await_args_list if c.args)
    assert "cancelled" in sql
    assert bridge._synced_test_run_ids == [11]


def test_stop_poll_default_and_clamp(monkeypatch):
    """T043：停止轮询默认 1000ms；非法值下限 100ms。"""
    import app.config as cfg
    from core.run_control import BatchStopWatcher

    monkeypatch.setattr(
        cfg, "get_settings", lambda: SimpleNamespace(exec_stop_poll_ms=1000)
    )
    assert BatchStopWatcher(None, 1)._poll_s == 1.0
    assert BatchStopWatcher(None, 1, poll_ms=0)._poll_s == 0.1
    assert BatchStopWatcher(None, 1, poll_ms=250)._poll_s == 0.25
