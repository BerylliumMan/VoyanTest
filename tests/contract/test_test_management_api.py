"""050：测试管理闭环 API 契约测试（本地 sqlite，无网络）。"""

from __future__ import annotations

import pytest
import pytest_asyncio

from app import db_models
from app.auth import hash_password

pytestmark = pytest.mark.contract


@pytest.fixture(autouse=True)
def _mark_db_ready(db):
    """契约 conftest 用普通 sessionmaker 覆盖 AsyncSessionLocal；恢复 is_ready 语义。"""
    import app.database as db_mod

    previous = db_mod.AsyncSessionLocal
    wrapper = db_mod._LazySessionMaker()
    wrapper.configure(previous)
    db_mod.AsyncSessionLocal = wrapper
    yield
    db_mod.AsyncSessionLocal = previous


@pytest_asyncio.fixture
async def plan_case(db, sample_project):
    case = db_models.TestCase(
        project_id=sample_project["id"], name="下单成功", project_case_number=1
    )
    db.add(case)
    await db.commit()
    await db.refresh(case)
    return case


@pytest_asyncio.fixture
async def reviewer(db, sample_project):
    user = db_models.User(
        username="reviewer1",
        password_hash=hash_password("Review@2024"),
        role="tester",
        status="active",
        must_change_password=False,
        project_ids=[sample_project["id"]],
        nickname="评审员",
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


def _plan_payload(project_id: int) -> dict:
    return {
        "project_id": project_id,
        "name": "支付回归",
        "description": "P1 测试管理",
        "start_date": "2026-11-11",
        "due_date": "2026-11-30",
    }


def test_plan_items_and_progress(client, admin_cookies, sample_project, plan_case):
    resp = client.post("/api/plans", json=_plan_payload(sample_project["id"]),
                       cookies=admin_cookies)
    assert resp.status_code == 201, resp.text
    plan = resp.json()
    assert plan["status"] == "draft"

    resp = client.post(f"/api/plans/{plan['id']}/items",
                       json={"case_ids": [plan_case.id]}, cookies=admin_cookies)
    assert resp.status_code == 200, resp.text
    assert resp.json()["added"] == 1

    # 重复加入被跳过
    resp = client.post(f"/api/plans/{plan['id']}/items",
                       json={"case_ids": [plan_case.id]}, cookies=admin_cookies)
    assert resp.json()["skipped"] == 1

    resp = client.patch(f"/api/plans/{plan['id']}", json={"status": "active"},
                        cookies=admin_cookies)
    assert resp.status_code == 200
    items = client.get(f"/api/plans/{plan['id']}/items", cookies=admin_cookies).json()["items"]
    assert len(items) == 1

    resp = client.patch(f"/api/plans/{plan['id']}/items/{items[0]['id']}",
                        json={"status": "passed", "priority": "high"},
                        cookies=admin_cookies)
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "passed"

    detail = client.get(f"/api/plans/{plan['id']}", cookies=admin_cookies).json()
    assert detail["stats"]["progress"] == 100.0
    assert detail["status"] == "completed"


def test_cycle_create_and_start(client, admin_cookies, sample_project, plan_case, monkeypatch):
    import app.services.test_cycle as cycle_service

    calls = []
    monkeypatch.setattr(cycle_service, "spawn_cycle_run", lambda **kw: calls.append(kw))

    plan = client.post("/api/plans", json=_plan_payload(sample_project["id"]),
                       cookies=admin_cookies).json()
    client.patch(f"/api/plans/{plan['id']}", json={"status": "active"}, cookies=admin_cookies)
    client.post(f"/api/plans/{plan['id']}/items", json={"case_ids": [plan_case.id]},
                cookies=admin_cookies)

    resp = client.post(f"/api/plans/{plan['id']}/cycles", json={}, cookies=admin_cookies)
    assert resp.status_code == 201, resp.text
    cycle = resp.json()
    assert cycle["case_count"] == 1 and cycle["status"] == "planned"

    resp = client.post(f"/api/cycles/{cycle['id']}/start", cookies=admin_cookies)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "running" and body["batch_id"]
    assert calls and calls[0]["batch_id"] == body["batch_id"]

    # 同一计划再次启动被拒绝
    cycle2 = client.post(f"/api/plans/{plan['id']}/cycles", json={},
                         cookies=admin_cookies).json()
    resp = client.post(f"/api/cycles/{cycle2['id']}/start", cookies=admin_cookies)
    assert resp.status_code == 409


def test_comments_and_mentions(client, admin_cookies, sample_project, plan_case, reviewer):
    resp = client.post("/api/comments", json={
        "entity_type": "case", "entity_id": plan_case.id,
        "body": f"@{reviewer.username} 这个断言要调整",
    }, cookies=admin_cookies)
    assert resp.status_code == 201, resp.text
    comment = resp.json()
    assert reviewer.id in comment["mentions"]

    resp = client.get("/api/comments", params={"entity_type": "case", "entity_id": plan_case.id},
                      cookies=admin_cookies)
    assert resp.status_code == 200
    assert len(resp.json()["items"]) == 1

    resp = client.delete(f"/api/comments/{comment['id']}", cookies=admin_cookies)
    assert resp.status_code == 200
    assert client.get("/api/comments", params={
        "entity_type": "case", "entity_id": plan_case.id,
    }, cookies=admin_cookies).json()["items"] == []


def test_case_review_flow(client, admin_cookies, sample_project, plan_case, reviewer):
    resp = client.post("/api/case-reviews", json={
        "case_id": plan_case.id, "reviewer_id": reviewer.id, "note": "请评审",
    }, cookies=admin_cookies)
    assert resp.status_code == 201, resp.text
    review = resp.json()
    assert review["status"] == "pending"

    resp = client.post("/api/case-reviews", json={
        "case_id": plan_case.id, "reviewer_id": reviewer.id,
    }, cookies=admin_cookies)
    assert resp.status_code == 409

    resp = client.get("/api/case-reviews", params={"mine_pending": True, "status": "pending"},
                      cookies=admin_cookies)
    assert resp.status_code == 200

    # 非评审人不能下结论
    resp = client.post(f"/api/case-reviews/{review['id']}/decision",
                       json={"status": "approved"}, cookies=admin_cookies)
    assert resp.status_code == 403

    resp = client.post(f"/api/case-reviews/{review['id']}/cancel", cookies=admin_cookies)
    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled"


def test_my_tasks(client, admin_cookies, sample_project, plan_case, reviewer):
    plan = client.post("/api/plans", json=_plan_payload(sample_project["id"]),
                       cookies=admin_cookies).json()
    client.patch(f"/api/plans/{plan['id']}", json={"status": "active"}, cookies=admin_cookies)
    client.post(f"/api/plans/{plan['id']}/items", json={"case_ids": [plan_case.id]},
                cookies=admin_cookies)
    item = client.get(f"/api/plans/{plan['id']}/items", cookies=admin_cookies).json()["items"][0]
    client.patch(f"/api/plans/{plan['id']}/items/{item['id']}",
                 json={"assignee_id": reviewer.id, "due_date": "2026-11-01"},
                 cookies=admin_cookies)

    # reviewer 登录查看自己的待办
    resp = client.post("/api/auth/login", json={
        "username": "reviewer1", "password": "Review@2024",
    })
    assert resp.status_code == 200, resp.text
    resp = client.get("/api/my/tasks", cookies=resp.cookies)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["plan_name"] == "支付回归"
    assert body["items"][0]["overdue"] is True
