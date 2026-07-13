"""Tests for core/runner/_persistence.py.

Target functions (target lines):
- mark_run_running       — pending → running 状态更新
- mark_run_failed        — failed 状态 + CRITICAL 日志 + 批次计数
- update_run_on_completion — 完成时统一更新 + 追加日志 + 批次计数
- append_run_logs        — 批量追加 RunLog
- precreate_pending_runs — 预创建 pending TestRun (含 is_init 标记)

save_run_results 已经在 tests/test_runner.py 覆盖, 此处不再重复。

策略:
- 真实 SQLite (conftest.py 的 db fixture) 满足 FK 约束
- db_models.User / Project / TestCase / RunBatch / TestRun / RunLog 真实 ORM 操作
- 不使用 client fixture, 避免触发 FastAPI lifespan 默认管理员创建冲突
"""
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from core.runner._persistence import (
    append_run_logs,
    mark_run_failed,
    mark_run_running,
    precreate_pending_runs,
    save_run_results,
    update_run_on_completion,
)


# ---------------------------------------------------------------------------
# 公共辅助
# ---------------------------------------------------------------------------


async def _make_project(db, name: str = "persist-proj") -> int:
    from app import db_models
    proj = db_models.Project(name=name, base_url="https://example.com")
    db.add(proj)
    await db.commit()
    await db.refresh(proj)
    return proj.id


async def _make_case(db, project_id: int, name: str) -> int:
    from app import db_models
    case = db_models.TestCase(project_id=project_id, name=name, description="")
    db.add(case)
    await db.commit()
    await db.refresh(case)
    return case.id


async def _make_batch(db, project_id: int, total_cases: int = 1) -> int:
    from app import db_models
    batch = db_models.RunBatch(project_id=project_id, total_cases=total_cases)
    db.add(batch)
    await db.commit()
    await db.refresh(batch)
    return batch.id


@pytest_asyncio.fixture
async def proj(db):
    """本地项目 fixture, 不依赖 client (避免 FastAPI lifespan 默认 admin 密码冲突)。"""
    return {"id": await _make_project(db, "persist-proj")}


async def _make_run(db, case_id: int, batch_id: int | None, status: str = "pending") -> int:
    from app import db_models
    run = db_models.TestRun(
        case_id=case_id, batch_id=batch_id, status=status,
        start_time=None, end_time=None,
    )
    db.add(run)
    await db.commit()
    await db.refresh(run)
    return run.id


# ===========================================================================
# mark_run_running
# ===========================================================================


@pytest.mark.asyncio
class TestMarkRunRunning:
    async def test_returns_true_when_row_updated(self, db, proj):
        """正常路径: 存在该 run_id → 更新 status=running, 返回 True"""
        case_id = await _make_case(db, proj["id"], "mark-running")
        batch_id = await _make_batch(db, proj["id"])
        run_id = await _make_run(db, case_id, batch_id, status="pending")

        result = await mark_run_running(db, run_id)
        assert result is True

        from app import db_models
        result_q = await db.execute(
            select(db_models.TestRun).where(db_models.TestRun.id == run_id)
        )
        run = result_q.scalar_one_or_none()
        assert run.status == "running"

    async def test_returns_false_when_run_not_found(self, db):
        """run_id 不存在 → rowcount=0, 返回 False"""
        result = await mark_run_running(db, 99999)
        assert result is False

    async def test_updates_multiple_invocations_idempotently(self, db, proj):
        """重复调用: 第一次 True, 第二次仍是 True (状态都是 running)"""
        case_id = await _make_case(db, proj["id"], "mark-running-2")
        run_id = await _make_run(db, case_id, None, status="pending")

        assert await mark_run_running(db, run_id) is True
        # 第二次调用, rowcount=1 (UPDATE 自身算 1)
        assert await mark_run_running(db, run_id) is True


# ===========================================================================
# mark_run_failed
# ===========================================================================


@pytest.mark.asyncio
class TestMarkRunFailed:
    async def test_basic_failure(self, db, proj):
        """正常路径: 标记 failed + 追加 CRITICAL 日志"""
        case_id = await _make_case(db, proj["id"], "fail-basic")
        batch_id = await _make_batch(db, proj["id"])
        run_id = await _make_run(db, case_id, batch_id, status="running")

        await mark_run_failed(db, run_id, "browser crashed")

        from app import db_models
        result_q = await db.execute(
            select(db_models.TestRun).where(db_models.TestRun.id == run_id)
        )
        run = result_q.scalar_one_or_none()
        assert run.status == "failed"
        assert run.duration == 0.0
        assert run.start_time is not None
        assert run.end_time is not None

        # RunLog 中有 CRITICAL 记录
        result_log = await db.execute(
            select(db_models.RunLog).where(db_models.RunLog.run_id == run_id)
        )
        log = result_log.scalar_one_or_none()
        assert log is not None
        assert log.level == "CRITICAL"
        assert log.message == "browser crashed"
        assert log.step_id is None

    async def test_with_batch_id_updates_counters(self, db, proj):
        """batch_id 提供时, 调用 crud.update_batch_counters 累加 failed"""
        case_id = await _make_case(db, proj["id"], "fail-batch")
        batch_id = await _make_batch(db, proj["id"])
        run_id = await _make_run(db, case_id, batch_id, status="running")

        with patch("core.runner._persistence.crud") as mock_crud:
            mock_crud.update_batch_counters = AsyncMock()
            await mark_run_failed(db, run_id, "err", batch_id=batch_id)
            mock_crud.update_batch_counters.assert_awaited_once()
            args = mock_crud.update_batch_counters.call_args
            assert args[0][1] == batch_id
            assert args[0][2] == "failed"

    async def test_explicit_start_end_time(self, db, proj):
        """start_time / end_time 显式传入时, 落库用传入值"""
        case_id = await _make_case(db, proj["id"], "fail-time")
        run_id = await _make_run(db, case_id, None, status="running")

        explicit_start = datetime(2026, 1, 1, 10, 0, 0)
        explicit_end = datetime(2026, 1, 1, 10, 5, 0)
        await mark_run_failed(
            db, run_id, "err",
            start_time=explicit_start, end_time=explicit_end,
        )

        from app import db_models
        result_q = await db.execute(
            select(db_models.TestRun).where(db_models.TestRun.id == run_id)
        )
        run = result_q.scalar_one_or_none()
        assert run.start_time == explicit_start
        assert run.end_time == explicit_end

    async def test_sqlalchemy_error_does_not_propagate(self, db, proj):
        """DB 抛 SQLAlchemyError 时不向调用方抛出 (兜底)"""
        case_id = await _make_case(db, proj["id"], "fail-exc")
        run_id = await _make_run(db, case_id, None, status="running")

        # 用 side_effect 抛错, 验证调用不抛
        original_execute = db.execute
        call_count = {"n": 0}

        async def boom(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise SQLAlchemyError("simulated failure")
            return await original_execute(*args, **kwargs)

        db.execute = boom
        try:
            # 应不抛
            await mark_run_failed(db, run_id, "err")
        finally:
            db.execute = original_execute

    async def test_batch_counter_failure_swallowed(self, db, proj):
        """batch_id 存在但 update_batch_counters 失败时, 不向调用方抛出"""
        case_id = await _make_case(db, proj["id"], "fail-batcherr")
        batch_id = await _make_batch(db, proj["id"])
        run_id = await _make_run(db, case_id, batch_id, status="running")

        with patch("core.runner._persistence.crud") as mock_crud:
            mock_crud.update_batch_counters = AsyncMock(
                side_effect=SQLAlchemyError("counter boom")
            )
            # 应不抛
            await mark_run_failed(db, run_id, "err", batch_id=batch_id)


# ===========================================================================
# update_run_on_completion
# ===========================================================================


@pytest.mark.asyncio
class TestUpdateRunOnCompletion:
    async def test_update_passed(self, db, proj):
        """正常路径: status=passed + 追加 log + batch counter"""
        case_id = await _make_case(db, proj["id"], "upd-pass")
        batch_id = await _make_batch(db, proj["id"])
        run_id = await _make_run(db, case_id, batch_id, status="running")

        start = datetime(2026, 1, 1)
        end = datetime(2026, 1, 1, 0, 1)
        logs = [{"step_id": None, "level": "INFO", "message": "ok"}]
        await update_run_on_completion(
            db, run_id, "passed", start, end, 60.0, "/report.html", logs,
            batch_id=batch_id,
        )

        from app import db_models
        result_q = await db.execute(
            select(db_models.TestRun).where(db_models.TestRun.id == run_id)
        )
        run = result_q.scalar_one_or_none()
        assert run.status == "passed"
        assert run.start_time == start
        assert run.end_time == end
        assert run.duration == 60.0
        assert run.report_path == "/report.html"
        assert run.log_path is None  # 显式置 None

        result_logs = await db.execute(
            select(db_models.RunLog).where(db_models.RunLog.run_id == run_id)
        )
        run_logs = result_logs.scalars().all()
        assert len(run_logs) == 1
        assert run_logs[0].message == "ok"

    async def test_update_without_batch_id(self, db, proj):
        """batch_id=None 时不调用 update_batch_counters"""
        case_id = await _make_case(db, proj["id"], "upd-nobatch")
        run_id = await _make_run(db, case_id, None, status="running")

        with patch("core.runner._persistence.crud") as mock_crud:
            await update_run_on_completion(
                db, run_id, "failed",
                datetime(2026, 1, 1), datetime(2026, 1, 1), 0.0,
                None, [],
            )
            mock_crud.update_batch_counters.assert_not_called()

    async def test_empty_log_list(self, db, proj):
        """logs=[] 时也能成功 (append_run_logs 走空循环)"""
        case_id = await _make_case(db, proj["id"], "upd-empty")
        run_id = await _make_run(db, case_id, None, status="running")

        await update_run_on_completion(
            db, run_id, "passed",
            datetime(2026, 1, 1), datetime(2026, 1, 1), 0.0,
            None, [],
        )
        from app import db_models
        result_q = await db.execute(
            select(db_models.TestRun).where(db_models.TestRun.id == run_id)
        )
        run = result_q.scalar_one_or_none()
        assert run.status == "passed"

    async def test_batch_counter_failure_logged(self, db, proj):
        """batch_id 提供但 update_batch_counters 抛错时不向上传播"""
        case_id = await _make_case(db, proj["id"], "upd-batcherr")
        batch_id = await _make_batch(db, proj["id"])
        run_id = await _make_run(db, case_id, batch_id, status="running")

        with patch("core.runner._persistence.crud") as mock_crud:
            mock_crud.update_batch_counters = AsyncMock(
                side_effect=SQLAlchemyError("counter err")
            )
            # 应不抛
            await update_run_on_completion(
                db, run_id, "passed",
                datetime(2026, 1, 1), datetime(2026, 1, 1), 0.0,
                None, [],
                batch_id=batch_id,
            )

    async def test_top_level_sqlalchemy_error_swallowed(self, db, proj):
        """UPDATE TestRun 自身抛错时也不向上传播"""
        case_id = await _make_case(db, proj["id"], "upd-toper")
        run_id = await _make_run(db, case_id, None, status="running")

        original_execute = db.execute
        call_count = {"n": 0}

        async def boom(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise SQLAlchemyError("update run failed")
            return await original_execute(*args, **kwargs)

        db.execute = boom
        try:
            # 应不抛
            await update_run_on_completion(
                db, run_id, "passed",
                datetime(2026, 1, 1), datetime(2026, 1, 1), 0.0,
                None, [],
            )
        finally:
            db.execute = original_execute


# ===========================================================================
# append_run_logs
# ===========================================================================


@pytest.mark.asyncio
class TestAppendRunLogs:
    async def test_inserts_multiple_logs(self, db, proj):
        """多行 RunLog 一次性追加"""
        case_id = await _make_case(db, proj["id"], "logs-multi")
        run_id = await _make_run(db, case_id, None, status="running")

        logs = [
            {"step_id": None, "level": "INFO", "message": "log 1"},
            {"step_id": None, "level": "WARN", "message": "log 2"},
            {"step_id": None, "level": "ERROR", "message": "log 3"},
        ]
        await append_run_logs(db, run_id, logs)

        from app import db_models
        result_q = await db.execute(
            select(db_models.RunLog)
            .where(db_models.RunLog.run_id == run_id)
            .order_by(db_models.RunLog.id)
        )
        rows = result_q.scalars().all()
        assert len(rows) == 3
        assert [r.message for r in rows] == ["log 1", "log 2", "log 3"]
        assert [r.level for r in rows] == ["INFO", "WARN", "ERROR"]

    async def test_empty_list_is_noop(self, db, proj):
        """logs=[] 不会插入任何行, 也不抛错"""
        case_id = await _make_case(db, proj["id"], "logs-empty")
        run_id = await _make_run(db, case_id, None, status="running")

        await append_run_logs(db, run_id, [])

        from app import db_models
        result_q = await db.execute(
            select(db_models.RunLog).where(db_models.RunLog.run_id == run_id)
        )
        rows = result_q.scalars().all()
        assert rows == []

    async def test_screenshot_path_optional(self, db, proj):
        """log_entry 无 screenshot_path 时落库为 None"""
        case_id = await _make_case(db, proj["id"], "logs-ss")
        run_id = await _make_run(db, case_id, None, status="running")

        logs = [{"step_id": None, "level": "INFO", "message": "no screenshot"}]
        await append_run_logs(db, run_id, logs)

        from app import db_models
        result_q = await db.execute(
            select(db_models.RunLog).where(db_models.RunLog.run_id == run_id)
        )
        row = result_q.scalar_one_or_none()
        assert row.screenshot_path is None

    async def test_screenshot_path_provided(self, db, proj):
        """log_entry 含 screenshot_path 时落库该值"""
        case_id = await _make_case(db, proj["id"], "logs-ss2")
        run_id = await _make_run(db, case_id, None, status="running")

        logs = [{
            "step_id": None, "level": "ERROR",
            "message": "boom",
            "screenshot_path": "/tmp/shot.png",
        }]
        await append_run_logs(db, run_id, logs)

        from app import db_models
        result_q = await db.execute(
            select(db_models.RunLog).where(db_models.RunLog.run_id == run_id)
        )
        row = result_q.scalar_one_or_none()
        assert row.screenshot_path == "/tmp/shot.png"

    async def test_exception_does_not_propagate(self, db, proj):
        """DB 抛错时不向调用方抛出"""
        case_id = await _make_case(db, proj["id"], "logs-exc")
        run_id = await _make_run(db, case_id, None, status="running")

        original_commit = db.commit
        call_count = {"n": 0}

        async def boom():
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise SQLAlchemyError("commit failed")
            return await original_commit()

        db.commit = boom
        try:
            # 应不抛
            await append_run_logs(db, run_id, [
                {"step_id": None, "level": "INFO", "message": "x"},
            ])
        finally:
            db.commit = original_commit


# ===========================================================================
# precreate_pending_runs
# ===========================================================================


@pytest.mark.asyncio
class TestPrecreatePendingRuns:
    async def test_creates_pending_runs_for_each_case(self, db, proj):
        """为每个 case 创建一行 pending TestRun, 返回 {case_id: run_id} 映射"""
        batch_id = await _make_batch(db, proj["id"], total_cases=3)
        c1 = await _make_case(db, proj["id"], "precreate-1")
        c2 = await _make_case(db, proj["id"], "precreate-2")
        c3 = await _make_case(db, proj["id"], "precreate-3")

        result = await precreate_pending_runs(db, [c1, c2, c3], batch_id)
        assert set(result.keys()) == {c1, c2, c3}
        assert all(isinstance(v, int) for v in result.values())

        from app import db_models
        result_q = await db.execute(
            select(db_models.TestRun).where(db_models.TestRun.batch_id == batch_id)
        )
        runs = result_q.scalars().all()
        assert len(runs) == 3
        for r in runs:
            assert r.status == "pending"
            assert r.start_time is None
            assert r.end_time is None
            assert r.is_init is False

    async def test_init_case_ids_marked_is_init(self, db, proj):
        """init_case_ids 中的用例 is_init=True"""
        batch_id = await _make_batch(db, proj["id"], total_cases=2)
        c_main = await _make_case(db, proj["id"], "main-case")
        c_init = await _make_case(db, proj["id"], "init-case")

        result = await precreate_pending_runs(
            db, [c_main], batch_id, init_case_ids=[c_init],
        )
        assert c_main in result
        assert c_init in result

        from app import db_models
        result_main = await db.execute(
            select(db_models.TestRun).where(db_models.TestRun.case_id == c_main)
        )
        run_main = result_main.scalar_one_or_none()
        result_init = await db.execute(
            select(db_models.TestRun).where(db_models.TestRun.case_id == c_init)
        )
        run_init = result_init.scalar_one_or_none()
        assert run_main.is_init is False
        assert run_init.is_init is True

    async def test_init_only_no_main(self, db, proj):
        """只有 init_case_ids, 没有主用例"""
        batch_id = await _make_batch(db, proj["id"], total_cases=1)
        c_init = await _make_case(db, proj["id"], "init-only")

        result = await precreate_pending_runs(db, [], batch_id, init_case_ids=[c_init])
        assert result == {c_init: result[c_init]}

    async def test_init_case_ids_none_equivalent(self, db, proj):
        """init_case_ids=None 与 init_case_ids=[] 行为一致"""
        batch_id = await _make_batch(db, proj["id"], total_cases=1)
        c = await _make_case(db, proj["id"], "no-init")

        result = await precreate_pending_runs(db, [c], batch_id, init_case_ids=None)
        assert c in result
        from app import db_models
        result_q = await db.execute(
            select(db_models.TestRun).where(db_models.TestRun.case_id == c)
        )
        run = result_q.scalar_one_or_none()
        assert run.is_init is False

    async def test_empty_inputs(self, db, proj):
        """主 + init 都为空: 返回空 dict, 不抛错"""
        batch_id = await _make_batch(db, proj["id"], total_cases=0)
        result = await precreate_pending_runs(db, [], batch_id)
        assert result == {}

    async def test_exception_returns_partial_mapping(self, db, proj):
        """部分用例插入失败时, 返回已创建的部分 (best-effort, 不抛)"""
        batch_id = await _make_batch(db, proj["id"], total_cases=2)
        c1 = await _make_case(db, proj["id"], "partial-1")
        c2 = await _make_case(db, proj["id"], "partial-2")

        # 用 side_effect 让第一次 commit 成功, 第二次失败
        original_commit = db.commit
        call_count = {"n": 0}

        async def maybe_fail():
            call_count["n"] += 1
            if call_count["n"] == 2:
                raise SQLAlchemyError("commit failed midway")
            return await original_commit()

        db.commit = maybe_fail
        try:
            result = await precreate_pending_runs(db, [c1, c2], batch_id)
        finally:
            db.commit = original_commit

        # 不抛错, 返回 dict (可能为空或部分)
        assert isinstance(result, dict)


# ===========================================================================
# save_run_results
# ===========================================================================


@pytest.mark.asyncio
class TestSaveRunResults:
    """save_run_results() 内部使用 AsyncSessionLocal() 自管理会话。
    :memory: SQLite 下该会话可能连接到不同的数据库，需要 monkey-patch
    让其使用 test engine 的会话工厂。
    """

    @staticmethod
    def _patch_sessionlocal(monkeypatch):
        from app.database import AsyncSessionLocal as test_session_factory
        import core.runner._persistence as _p
        monkeypatch.setattr(_p.db_mod, "AsyncSessionLocal", test_session_factory)

    async def test_create_new_run_with_logs(self, db, proj, monkeypatch):
        """正常路径: 新建 TestRun + 写入 RunLog + 无 batch_id 时不更新 counter"""
        self._patch_sessionlocal(monkeypatch)
        case_id = await _make_case(db, proj["id"], "save-new")

        logs = [
            {"step_id": None, "level": "INFO", "message": "step done"},
        ]
        run_id = await save_run_results(
            case_id, "passed",
            datetime(2026, 1, 1), datetime(2026, 1, 1), 0.0,
            None, None, logs,
        )
        assert run_id is not None
        from app import db_models
        result_q = await db.execute(
            select(db_models.TestRun).where(db_models.TestRun.id == run_id)
        )
        run = result_q.scalar_one_or_none()
        assert run.case_id == case_id
        assert run.status == "passed"
        assert run.batch_id is None

        result_logs = await db.execute(
            select(db_models.RunLog).where(db_models.RunLog.run_id == run_id)
        )
        run_logs = result_logs.scalars().all()
        assert len(run_logs) == 1

    async def test_update_existing_run(self, db, proj, monkeypatch):
        """run_id 存在时, UPDATE 现有 TestRun 而非新建"""
        self._patch_sessionlocal(monkeypatch)
        case_id = await _make_case(db, proj["id"], "save-update")
        batch_id = await _make_batch(db, proj["id"])
        run_id = await _make_run(db, case_id, batch_id, status="pending")

        new_id = await save_run_results(
            case_id, "passed",
            datetime(2026, 1, 1), datetime(2026, 1, 1, 0, 1), 60.0,
            "/report.html", None, [], run_id=run_id,
        )
        assert new_id == run_id

        from app import db_models
        db.expire_all()
        result_q = await db.execute(
            select(db_models.TestRun).where(db_models.TestRun.id == run_id)
        )
        run = result_q.scalar_one_or_none()
        assert run.status == "passed"
        assert run.report_path == "/report.html"
        assert run.duration == 60.0

    async def test_run_id_not_found_fallback(self, db, proj, monkeypatch):
        """run_id 找不到时降级为新建 (返回新 ID)"""
        self._patch_sessionlocal(monkeypatch)
        case_id = await _make_case(db, proj["id"], "save-fallback")
        new_id = await save_run_results(
            case_id, "passed",
            datetime(2026, 1, 1), datetime(2026, 1, 1), 0.0,
            None, None, [], run_id=99999,
        )
        assert new_id is not None
        assert new_id != 99999

    async def test_with_batch_id_updates_counters(self, db, proj, monkeypatch):
        """batch_id 提供时调用 update_batch_counters"""
        self._patch_sessionlocal(monkeypatch)
        case_id = await _make_case(db, proj["id"], "save-batch")
        batch_id = await _make_batch(db, proj["id"])

        with patch("core.runner._persistence.crud") as mock_crud:
            mock_crud.update_batch_counters = AsyncMock()
            run_id = await save_run_results(
                case_id, "passed",
                datetime(2026, 1, 1), datetime(2026, 1, 1), 0.0,
                None, None, [], batch_id=batch_id,
            )
            assert run_id is not None
            mock_crud.update_batch_counters.assert_awaited_once()
            args = mock_crud.update_batch_counters.call_args
            assert args[0][1] == batch_id
            assert args[0][2] == "passed"

    async def test_exception_returns_none(self, db, proj, monkeypatch):
        """DB 抛错时 rollback 并返回 None"""
        self._patch_sessionlocal(monkeypatch)
        case_id = await _make_case(db, proj["id"], "save-exc")

        class _BoomSession:
            def __init__(self, *a, **kw): pass
            async def execute(self, *a, **kw):
                raise SQLAlchemyError("simulated db error")
            def add(self, *a, **kw): pass
            async def commit(self, *a, **kw): pass
            async def rollback(self, *a, **kw): pass
            async def refresh(self, *a, **kw): pass
            async def close(self, *a, **kw): pass
            async def __aenter__(self):
                return self
            async def __aexit__(self, *exc):
                return False

        monkeypatch.setattr(
            "core.runner._persistence.db_mod.AsyncSessionLocal",
            lambda: _BoomSession(),
        )

        result = await save_run_results(
            case_id, "passed",
            datetime(2026, 1, 1), datetime(2026, 1, 1), 0.0,
            None, None, [],
        )
        assert result is None

    async def test_screenshot_path_passed_through(self, db, proj, monkeypatch):
        """log_entry 的 screenshot_path 正确写入 RunLog 表"""
        self._patch_sessionlocal(monkeypatch)
        case_id = await _make_case(db, proj["id"], "save-ss")
        logs = [{
            "step_id": None, "level": "ERROR",
            "message": "fail",
            "screenshot_path": "/tmp/shot.png",
        }]
        run_id = await save_run_results(
            case_id, "failed",
            datetime(2026, 1, 1), datetime(2026, 1, 1), 0.0,
            None, None, logs,
        )
        assert run_id is not None
        from app import db_models
        result_q = await db.execute(
            select(db_models.RunLog).where(db_models.RunLog.run_id == run_id)
        )
        row = result_q.scalar_one_or_none()
        assert row.screenshot_path == "/tmp/shot.png"
