"""044 US4：记忆端点契约（权限 / 空列表 / 删除不存在）。"""

from __future__ import annotations

import pytest

from app import db_models


@pytest.fixture(autouse=True)
def _isolate_memory(tmp_path, monkeypatch):
    monkeypatch.setattr("core.chat.memory._ROOT", tmp_path)


@pytest.fixture(autouse=True)
def _mark_db_ready(db):
    import app.database as db_mod

    previous = db_mod.AsyncSessionLocal
    wrapper = db_mod._LazySessionMaker()
    wrapper.configure(previous)
    db_mod.AsyncSessionLocal = wrapper
    yield
    db_mod.AsyncSessionLocal = previous


async def _project(db):
    project = db_models.Project(name="044 记忆项目")
    db.add(project)
    await db.commit()
    await db.refresh(project)
    return project


@pytest.mark.asyncio
async def test_memory_list_empty(client, db, admin_cookies):
    project = await _project(db)
    resp = client.get(f"/api/chat/memory?project_id={project.id}", cookies=admin_cookies)
    assert resp.status_code == 200, resp.text
    assert resp.json()["items"] == []
    assert resp.json()["total"] == 0


@pytest.mark.asyncio
async def test_memory_delete_missing_returns_404(client, db, admin_cookies):
    project = await _project(db)
    resp = client.delete(
        f"/api/chat/memory/not-exists?project_id={project.id}", cookies=admin_cookies
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_memory_requires_auth(client):
    resp = client.get("/api/chat/memory?project_id=1")
    assert resp.status_code == 401
