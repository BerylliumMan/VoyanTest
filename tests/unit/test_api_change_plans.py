"""046：变更计划状态机测试（创建/合并/上限/认领/宽限/重试/取消/僵尸回收）。"""

from datetime import timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.models.api_extract import ApiChangeTrigger
from app.models.project import Project
from app.models.quality import QualityRegressionPlan
from app.services.api_change_impact import AffectedApi
from app.services.api_change_plans import (
    RETRY_BACKOFF_MINUTES,
    cancel_trigger,
    claim_trigger,
    create_change_plan,
    next_retry_at,
    scan_due_triggers,
)
from app.tz import now as tz_now


@pytest_asyncio.fixture
async def db():
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    import app.db_models  # noqa: F401
    from app.database import Base

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        yield session
    await engine.dispose()


@pytest_asyncio.fixture
async def project_id(db):
    project = Project(name="plan-demo")
    db.add(project)
    await db.commit()
    await db.refresh(project)
    return project.id


def _affected(definition_id: int = 1, case_ids: list[int] | None = None) -> AffectedApi:
    return AffectedApi(
        definition_id=definition_id,
        method="GET",
        path="/users",
        case_ids=list(case_ids or [101]),
        changed_files=["src/UserController.java"],
    )


async def _triggers(db):
    return (await db.execute(select(ApiChangeTrigger))).scalars().all()


@pytest.mark.asyncio
async def test_create_plan_with_cases(db, project_id):
    now = tz_now()
    result = await create_change_plan(
        db,
        project_id=project_id,
        affected=[_affected(1, [101, 102])],
        from_commit="aaa",
        to_commit="bbb",
        now=now,
    )
    await db.commit()

    assert result.created is True
    trigger = (await _triggers(db))[0]
    assert trigger.status == "pending"
    assert trigger.case_count == 2
    assert trigger.deadline_at.replace(tzinfo=None) == (
        now + timedelta(minutes=60)
    ).replace(tzinfo=None)

    plan = await db.get(QualityRegressionPlan, trigger.plan_id)
    assert plan is not None
    assert [item["case_id"] for item in plan.items] == [101, 102]
    assert all(item["case_kind"] == "api" for item in plan.items)
    assert plan.items[0]["reasons"]


@pytest.mark.asyncio
async def test_no_cases_creates_nothing(db, project_id):
    result = await create_change_plan(
        db,
        project_id=project_id,
        affected=[],
        from_commit="aaa",
        to_commit="bbb",
    )
    await db.commit()
    assert result.created is False
    assert result.reason == "no_cases"
    assert await _triggers(db) == []


@pytest.mark.asyncio
async def test_merge_same_from_commit_keeps_deadline(db, project_id):
    now = tz_now()
    first = await create_change_plan(
        db,
        project_id=project_id,
        affected=[_affected(1, [101])],
        from_commit="aaa",
        to_commit="bbb",
        now=now,
    )
    await db.commit()
    original_deadline = (await _triggers(db))[0].deadline_at

    second = await create_change_plan(
        db,
        project_id=project_id,
        affected=[_affected(2, [101, 202])],
        from_commit="aaa",
        to_commit="ccc",
        now=now + timedelta(minutes=10),
    )
    await db.commit()

    assert second.created is False and second.merged is True
    assert second.trigger_id == first.trigger_id
    triggers = await _triggers(db)
    assert len(triggers) == 1
    trigger = triggers[0]
    assert trigger.case_count == 2  # 101 去重
    assert trigger.to_commit == "ccc"
    assert trigger.deadline_at == original_deadline  # 不重置
    plan = await db.get(QualityRegressionPlan, trigger.plan_id)
    assert sorted(item["case_id"] for item in plan.items) == [101, 202]


@pytest.mark.asyncio
async def test_pending_limit_merges_into_latest(db, project_id):
    for i in range(3):
        await create_change_plan(
            db,
            project_id=project_id,
            affected=[_affected(i, [100 + i])],
            from_commit=f"base{i}",
            to_commit=f"head{i}",
            max_pending=3,
        )
        await db.commit()

    result = await create_change_plan(
        db,
        project_id=project_id,
        affected=[_affected(9, [999])],
        from_commit="base9",
        to_commit="head9",
        max_pending=3,
    )
    await db.commit()

    assert result.created is False
    assert result.merged is True
    assert result.reason == "pending_limit"
    triggers = await _triggers(db)
    assert len(triggers) == 3
    latest = max(triggers, key=lambda t: t.id)
    plan = await db.get(QualityRegressionPlan, latest.plan_id)
    assert 999 in {item["case_id"] for item in plan.items}


@pytest.mark.asyncio
async def test_claim_only_when_due(db, project_id):
    now = tz_now()
    await create_change_plan(
        db,
        project_id=project_id,
        affected=[_affected()],
        from_commit="a",
        to_commit="b",
        now=now,
    )
    await db.commit()
    trigger = (await _triggers(db))[0]

    assert await claim_trigger(db, trigger.id, now=now) is False  # 未到期
    assert await claim_trigger(db, trigger.id, now=now + timedelta(minutes=61)) is True
    await db.commit()
    await db.refresh(trigger)
    assert trigger.status == "running"
    assert trigger.claimed_at is not None

    # 二次认领失败（已不在 pending）
    assert await claim_trigger(db, trigger.id, now=now + timedelta(minutes=62)) is False


@pytest.mark.asyncio
async def test_expired_beyond_grace_is_skipped(db, project_id):
    now = tz_now()
    await create_change_plan(
        db,
        project_id=project_id,
        affected=[_affected()],
        from_commit="a",
        to_commit="b",
        now=now,
        window_minutes=60,
        grace_minutes=10,
    )
    await db.commit()

    # 超出宽限 5 分钟
    ids = await scan_due_triggers(db, now=now + timedelta(minutes=75), grace_minutes=10)
    await db.commit()
    assert ids == []
    trigger = (await _triggers(db))[0]
    assert trigger.status == "expired_skipped"
    assert trigger.decision == "expired_skipped"


@pytest.mark.asyncio
async def test_within_grace_still_claimable(db, project_id):
    now = tz_now()
    await create_change_plan(
        db,
        project_id=project_id,
        affected=[_affected()],
        from_commit="a",
        to_commit="b",
        now=now,
        window_minutes=60,
        grace_minutes=10,
    )
    await db.commit()

    ids = await scan_due_triggers(db, now=now + timedelta(minutes=65), grace_minutes=10)
    await db.commit()
    assert len(ids) == 1
    trigger = (await _triggers(db))[0]
    assert trigger.status == "running"


@pytest.mark.asyncio
async def test_retry_backoff_is_bounded(db, project_id):
    now = tz_now()
    await create_change_plan(
        db,
        project_id=project_id,
        affected=[_affected()],
        from_commit="a",
        to_commit="b",
        now=now,
    )
    await db.commit()
    trigger = (await _triggers(db))[0]

    assert next_retry_at(trigger, now=now, retry_count=0) == now + timedelta(
        minutes=RETRY_BACKOFF_MINUTES[0]
    )
    assert next_retry_at(trigger, now=now, retry_count=1) == now + timedelta(
        minutes=RETRY_BACKOFF_MINUTES[1]
    )
    assert next_retry_at(trigger, now=now, retry_count=2) is None  # 不再重试


@pytest.mark.asyncio
async def test_cancel_trigger(db, project_id):
    await create_change_plan(
        db, project_id=project_id, affected=[_affected()], from_commit="a", to_commit="b"
    )
    await db.commit()
    trigger = (await _triggers(db))[0]

    assert await cancel_trigger(db, trigger.id, user_id=7, reason="不需要") is True
    await db.commit()
    await db.refresh(trigger)
    assert trigger.status == "cancelled"
    assert trigger.decision == "cancelled"
    assert trigger.decided_by == 7

    assert await cancel_trigger(db, trigger.id, user_id=7) is False


@pytest.mark.asyncio
async def test_zombie_running_recovered(db, project_id):
    now = tz_now()
    await create_change_plan(
        db,
        project_id=project_id,
        affected=[_affected()],
        from_commit="a",
        to_commit="b",
        now=now,
    )
    await db.commit()
    trigger = (await _triggers(db))[0]
    trigger.status = "running"
    trigger.claimed_at = now - timedelta(minutes=10)
    await db.commit()

    await scan_due_triggers(db, now=now, grace_minutes=10)
    await db.commit()
    await db.refresh(trigger)
    assert trigger.status == "failed"
    assert "进程" in (trigger.reason or "")


@pytest.mark.asyncio
async def test_execute_claimed_trigger_creates_batch(db, project_id, monkeypatch):
    from sqlalchemy import select as _select
    from sqlalchemy.ext.asyncio import async_sessionmaker

    import app.database as db_mod
    import core.runner
    from app.models.batch import RunBatch
    from app.services.api_change_plans import execute_claimed_trigger

    maker = async_sessionmaker(db.bind, expire_on_commit=False)
    monkeypatch.setattr(db_mod, "AsyncSessionLocal", maker)

    calls = []

    async def fake_run(case_ids, project_id_, **kwargs):
        calls.append((list(case_ids), project_id_, kwargs.get("batch_id")))

    monkeypatch.setattr(core.runner, "run_batch_test_cases", fake_run)

    now = tz_now()
    await create_change_plan(
        db,
        project_id=project_id,
        affected=[_affected(1, [101, 102])],
        from_commit="a",
        to_commit="b",
        now=now,
    )
    await db.commit()
    trigger = (await _triggers(db))[0]
    assert await claim_trigger(db, trigger.id, now=now + timedelta(minutes=61)) is True
    await db.commit()

    batch_id = await execute_claimed_trigger(trigger.id)
    assert batch_id is not None
    assert calls == [([101, 102], project_id, batch_id)]

    await db.refresh(trigger)
    assert trigger.status == "done"
    assert trigger.decision == "auto_ran"
    assert trigger.batch_id == batch_id

    batch = (
        await db.execute(_select(RunBatch).where(RunBatch.id == batch_id))
    ).scalar_one()
    assert batch.project_id == project_id
    assert batch.total_cases == 2
    assert batch.source == "api_change_trigger"


@pytest.mark.asyncio
async def test_execute_failure_schedules_retry(db, project_id, monkeypatch):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    import app.database as db_mod
    import core.runner
    from app.services.api_change_plans import execute_claimed_trigger

    maker = async_sessionmaker(db.bind, expire_on_commit=False)
    monkeypatch.setattr(db_mod, "AsyncSessionLocal", maker)

    async def boom(case_ids, project_id_, **kwargs):
        raise RuntimeError("runner exploded")

    monkeypatch.setattr(core.runner, "run_batch_test_cases", boom)

    now = tz_now()
    await create_change_plan(
        db,
        project_id=project_id,
        affected=[_affected(1, [101])],
        from_commit="a",
        to_commit="b",
        now=now,
    )
    await db.commit()
    trigger = (await _triggers(db))[0]
    assert await claim_trigger(db, trigger.id, now=now + timedelta(minutes=61)) is True
    await db.commit()

    await execute_claimed_trigger(trigger.id)
    await db.refresh(trigger)
    assert trigger.status == "pending"
    assert trigger.retry_count == 1
    assert trigger.retry_at is not None
    assert "runner exploded" in (trigger.reason or "")


@pytest.mark.asyncio
async def test_execute_failure_after_max_retries_marks_failed(db, project_id, monkeypatch):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    import app.database as db_mod
    import core.runner
    from app.services.api_change_plans import execute_claimed_trigger

    maker = async_sessionmaker(db.bind, expire_on_commit=False)
    monkeypatch.setattr(db_mod, "AsyncSessionLocal", maker)

    async def boom(case_ids, project_id_, **kwargs):
        raise RuntimeError("still failing")

    monkeypatch.setattr(core.runner, "run_batch_test_cases", boom)

    now = tz_now()
    await create_change_plan(
        db,
        project_id=project_id,
        affected=[_affected(1, [101])],
        from_commit="a",
        to_commit="b",
        now=now,
    )
    await db.commit()
    trigger = (await _triggers(db))[0]
    trigger.retry_count = len(RETRY_BACKOFF_MINUTES)
    assert await claim_trigger(db, trigger.id, now=now + timedelta(minutes=61)) is True
    await db.commit()

    await execute_claimed_trigger(trigger.id)
    await db.refresh(trigger)
    assert trigger.status == "failed"
    assert trigger.decision == "failed"


@pytest.mark.asyncio
async def test_project_confirm_window_overrides_global(db, project_id):
    from app.models.api_extract import CodeRepoBinding
    from app.models.api_test import ApiDefinition
    from app.models.testcase import TestCase
    from app.services.api_change_plans import plan_after_sync
    from app.services.api_sync import SyncOutcome

    binding = CodeRepoBinding(
        project_id=project_id,
        repo_url="/tmp/x",
        branch="",
        extractor="auto",
        confirm_window_minutes=30,
    )
    db.add(binding)
    definition = ApiDefinition(
        project_id=project_id,
        name="GET /a",
        method="GET",
        path="/a",
        source="spring",
        source_file="src/A.java",
        extractor="spring_source",
        status="active",
    )
    db.add(definition)
    await db.commit()
    await db.refresh(binding)
    await db.refresh(definition)
    case = TestCase(
        project_id=project_id,
        name="case",
        case_kind="api",
        api_definition_id=definition.id,
        api_spec={"steps": []},
        status="active",
    )
    db.add(case)
    await db.commit()

    now = tz_now()
    outcome = SyncOutcome(
        status="success",
        base_commit="base",
        head_commit="head",
        changed_files=["src/A.java"],
    )
    result = await plan_after_sync(db, binding, outcome)
    assert result["status"] == "planned"

    trigger = (await _triggers(db))[0]
    delta = trigger.deadline_at.replace(tzinfo=None) - now.replace(tzinfo=None)
    assert 29 <= delta.total_seconds() / 60 <= 31


@pytest.mark.asyncio
async def test_project_window_inherits_global_when_unset(db, project_id):
    from app.models.api_extract import CodeRepoBinding
    from app.models.api_test import ApiDefinition
    from app.models.testcase import TestCase
    from app.services.api_change_plans import plan_after_sync
    from app.services.api_sync import SyncOutcome

    binding = CodeRepoBinding(
        project_id=project_id, repo_url="/tmp/x", branch="", extractor="auto"
    )
    db.add(binding)
    definition = ApiDefinition(
        project_id=project_id,
        name="GET /a",
        method="GET",
        path="/a",
        source="spring",
        source_file="src/A.java",
        extractor="spring_source",
        status="active",
    )
    db.add(definition)
    await db.commit()
    await db.refresh(binding)
    await db.refresh(definition)
    db.add(
        TestCase(
            project_id=project_id,
            name="case",
            case_kind="api",
            api_definition_id=definition.id,
            api_spec={"steps": []},
            status="active",
        )
    )
    await db.commit()

    now = tz_now()
    outcome = SyncOutcome(
        status="success", base_commit="base", head_commit="head", changed_files=["src/A.java"]
    )
    await plan_after_sync(db, binding, outcome)
    trigger = (await _triggers(db))[0]
    delta = trigger.deadline_at.replace(tzinfo=None) - now.replace(tzinfo=None)
    assert 59 <= delta.total_seconds() / 60 <= 61
