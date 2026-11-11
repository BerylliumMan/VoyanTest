"""050：测试管理闭环 —— 计划/轮次/评论/评审/待办服务层单测。"""

from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi import HTTPException

from app.models.auth import User
from app.models.batch import RunBatch
from app.models.project import Project
from app.models.testcase import TestCase
from app.services import case_review as review_service
from app.services import comments as comment_service
from app.services import my_tasks as my_tasks_service
from app.services import test_cycle as cycle_service
from app.services import test_plan as plan_service


@pytest_asyncio.fixture
async def db():
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    import app.db_models  # noqa: F401  注册全部模型
    from app.database import Base

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        yield session
    await engine.dispose()


@pytest_asyncio.fixture
async def ctx(db):
    project = Project(name="测试管理项目")
    db.add(project)
    await db.flush()
    bob = User(
        username="bob", password_hash="x", role="tester",
        status="active", project_ids=[project.id], nickname="小波",
    )
    alice = User(
        username="alice", password_hash="x", role="tester",
        status="active", project_ids=[project.id], nickname="爱丽丝",
    )
    db.add_all([bob, alice])
    await db.flush()
    admin = User(username="admin", password_hash="x", role="admin", status="active")
    db.add(admin)
    await db.flush()

    cases = []
    for i in range(2):
        case = TestCase(project_id=project.id, name=f"用例{i + 1}", project_case_number=i + 1)
        db.add(case)
        cases.append(case)
    await db.flush()
    await db.commit()
    return SimpleNamespace(
        db=db, project=project, bob=bob, alice=alice, admin=admin, cases=cases
    )


@pytest.mark.asyncio
async def test_plan_items_and_auto_complete(ctx):
    db, admin = ctx.db, ctx.admin
    plan = await plan_service.create_plan(db, admin, {
        "project_id": ctx.project.id, "name": "支付回归",
        "description": "回归", "start_date": date.today(),
        "due_date": date.today() + timedelta(days=7),
    })
    assert plan["status"] == "draft"
    added = await plan_service.add_items(db, admin, plan["id"], [ctx.cases[0].id, ctx.cases[1].id])
    assert added["added"] == 2
    dup = await plan_service.add_items(db, admin, plan["id"], [ctx.cases[0].id])
    assert dup["added"] == 0 and dup["skipped"] == 1

    detail = await plan_service.get_plan_detail(db, plan["id"])
    assert detail["stats"]["total"] == 2
    assert detail["stats"]["not_started"] == 2

    await plan_service.update_plan(db, admin, plan["id"], {"status": "active"})
    items = await plan_service.list_items(db, admin, plan["id"])
    for item in items:
        await plan_service.update_item(db, admin, plan["id"], item["id"], {"status": "passed"})
    detail = await plan_service.get_plan_detail(db, plan["id"])
    assert detail["status"] == "completed"
    assert detail["stats"]["progress"] == 100.0


@pytest.mark.asyncio
async def test_plan_project_access_is_enforced(ctx):
    db = ctx.db
    outsider = SimpleNamespace(id=999, role="tester", project_ids=[])
    with pytest.raises(HTTPException) as exc:
        await plan_service.create_plan(db, outsider, {
            "project_id": ctx.project.id, "name": "越权计划",
        })
    assert exc.value.status_code == 403

    with pytest.raises(HTTPException) as exc2:
        await plan_service.create_plan(db, ctx.admin, {"project_id": 99999, "name": "x"})
    assert exc2.value.status_code == 404


@pytest.mark.asyncio
async def test_cycle_snapshot_start_and_conflict(ctx, monkeypatch):
    db, admin = ctx.db, ctx.admin
    plan = await plan_service.create_plan(db, admin, {
        "project_id": ctx.project.id, "name": "轮次计划",
    })
    await plan_service.update_plan(db, admin, plan["id"], {"status": "active"})
    await plan_service.add_items(db, admin, plan["id"], [c.id for c in ctx.cases])

    spawned = []
    monkeypatch.setattr(cycle_service, "spawn_cycle_run", lambda **kw: spawned.append(kw))

    cycle = await cycle_service.create_cycle(db, admin, plan["id"], {})
    assert cycle["case_count"] == 2 and cycle["status"] == "planned"

    started = await cycle_service.start_cycle(db, admin, cycle["id"])
    assert started["status"] == "running"
    assert spawned and spawned[0]["case_ids"]
    batch = await db.get(RunBatch, started["batch_id"])
    assert batch.source == "test_cycle" and batch.source_id == cycle["id"]

    with pytest.raises(HTTPException) as exc:
        await cycle_service.start_cycle(db, admin, cycle["id"])
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_comments_mention_notifies_user(ctx):
    db, alice, bob = ctx.db, ctx.alice, ctx.bob
    case = ctx.cases[0]
    created = await comment_service.create_comment(
        db, alice, entity_type="case", entity_id=case.id,
        body="@小波 这个断言是不是写反了？",
    )
    assert bob.id in created["mentions"]
    assert created["user_name"] == "爱丽丝"

    from sqlalchemy import select
    from app.models.notification import Notification

    notes = (
        await db.execute(select(Notification).where(Notification.user_id == bob.id))
    ).scalars().all()
    assert len(notes) == 1
    assert "提到了你" in notes[0].title

    listed = await comment_service.list_comments(db, bob, entity_type="case", entity_id=case.id)
    assert len(listed) == 1
    assert listed[0]["can_edit"] is False


@pytest.mark.asyncio
async def test_case_review_pending_unique_and_decision(ctx):
    db, alice, bob = ctx.db, ctx.alice, ctx.bob
    case = ctx.cases[0]
    review = await review_service.create_review(
        db, alice, case_id=case.id, reviewer_id=bob.id, note="请确认步骤"
    )
    assert review["status"] == "pending"

    with pytest.raises(HTTPException) as exc:
        await review_service.create_review(db, alice, case_id=case.id, reviewer_id=bob.id)
    assert exc.value.status_code == 409

    decided = await review_service.decide_review(
        db, bob, review["id"], status="approved", comment="OK"
    )
    assert decided["status"] == "approved" and decided["decided_at"] is not None

    with pytest.raises(HTTPException) as exc2:
        await review_service.decide_review(db, bob, review["id"], status="approved")
    assert exc2.value.status_code == 409

    # reviewer != requester 校验
    with pytest.raises(HTTPException) as exc3:
        await review_service.create_review(db, alice, case_id=ctx.cases[1].id, reviewer_id=alice.id)
    assert exc3.value.status_code == 400


@pytest.mark.asyncio
async def test_my_tasks_filters(ctx):
    db, admin, bob = ctx.db, ctx.admin, ctx.bob
    plan = await plan_service.create_plan(db, admin, {
        "project_id": ctx.project.id, "name": "待办计划",
    })
    await plan_service.update_plan(db, admin, plan["id"], {"status": "active"})
    await plan_service.add_items(db, admin, plan["id"], [c.id for c in ctx.cases])
    items = await plan_service.list_items(db, admin, plan["id"])
    yesterday = date.today() - timedelta(days=1)
    await plan_service.update_item(db, admin, plan["id"], items[0]["id"], {
        "assignee_id": bob.id, "due_date": yesterday, "priority": "high",
    })
    await plan_service.update_item(db, admin, plan["id"], items[1]["id"], {
        "assignee_id": bob.id, "status": "passed",
    })

    todo = await my_tasks_service.list_my_tasks(db, bob)
    assert todo["total"] == 1 and todo["items"][0]["overdue"] is True

    overdue = await my_tasks_service.list_my_tasks(db, bob, filter="overdue")
    assert overdue["total"] == 1

    done = await my_tasks_service.list_my_tasks(db, bob, filter="done")
    assert done["total"] == 1 and done["items"][0]["status"] == "passed"


@pytest.mark.asyncio
async def test_sync_cycle_maps_run_status_and_plan_items(ctx, monkeypatch):
    db, admin = ctx.db, ctx.admin
    plan = await plan_service.create_plan(db, admin, {
        "project_id": ctx.project.id, "name": "回填计划",
    })
    await plan_service.update_plan(db, admin, plan["id"], {"status": "active"})
    await plan_service.add_items(db, admin, plan["id"], [ctx.cases[0].id, ctx.cases[1].id])
    monkeypatch.setattr(cycle_service, "spawn_cycle_run", lambda **kw: None)
    cycle = await cycle_service.create_cycle(db, admin, plan["id"], {})
    started = await cycle_service.start_cycle(db, admin, cycle["id"])

    from app.models.batch import TestRun
    from app.tz import now as tz_now

    batch = await db.get(RunBatch, started["batch_id"])
    batch.status = "partial"
    batch.finished_at = tz_now()
    db.add_all([
        TestRun(batch_id=batch.id, case_id=ctx.cases[0].id, status="passed"),
        TestRun(batch_id=batch.id, case_id=ctx.cases[1].id, status="failed", failure_kind="assertion"),
    ])
    await db.commit()

    await cycle_service.sync_cycle_from_batch(db, batch)
    detail = await cycle_service.get_cycle_detail(db, admin, cycle["id"])
    assert detail["status"] == "completed"
    by_case = {i["case_id"]: i["run_status"] for i in detail["items"]}
    assert by_case[ctx.cases[0].id] == "passed"
    assert by_case[ctx.cases[1].id] == "failed"

    items = await plan_service.list_items(db, admin, plan["id"])
    by_name = {i["case_name"]: i["status"] for i in items}
    assert by_name["用例1"] == "passed" and by_name["用例2"] == "failed"
    assert (await plan_service.get_plan_detail(db, plan["id"]))["status"] == "active"


@pytest.mark.asyncio
async def test_cycle_retry_failed_updates_cycle(ctx, monkeypatch):
    db, admin = ctx.db, ctx.admin
    plan = await plan_service.create_plan(db, admin, {
        "project_id": ctx.project.id, "name": "重跑计划",
    })
    await plan_service.update_plan(db, admin, plan["id"], {"status": "active"})
    await plan_service.add_items(db, admin, plan["id"], [ctx.cases[0].id])
    monkeypatch.setattr(cycle_service, "spawn_cycle_run", lambda **kw: None)
    cycle = await cycle_service.create_cycle(db, admin, plan["id"], {})
    started = await cycle_service.start_cycle(db, admin, cycle["id"])

    import app.services.batch_rerun as rerun

    async def fake_rerun(db_, batch_, **kw):
        assert kw["environment_id"] is None
        return {"batch": SimpleNamespace(id=9001), "total": 1}

    monkeypatch.setattr(rerun, "create_rerun_batch", fake_rerun)
    out = await cycle_service.retry_failed(db, admin, cycle["id"])
    assert out == {"batch_id": 9001, "case_count": 1}
    refreshed = await db.get(cycle_service.db_models.TestCycle, cycle["id"])
    assert refreshed.batch_id == 9001 and refreshed.status == "running"
    assert started["batch_id"] != 9001
