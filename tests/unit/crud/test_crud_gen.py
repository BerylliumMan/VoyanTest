"""crud/gen.py 单元测试 — GenSession/GenFunctionalPoint/GenTestCase CRUD。"""
import pytest
from sqlalchemy import select

from app import crud, db_models


@pytest.mark.asyncio
async def test_create_and_get_session(db):
    session = await crud.create_gen_session(
        db, session_id="test-session-1", filename="test.docx",
        filenames='["test.docx"]', project_id=1,
        project_description="测试项目", status="analyzing",
    )
    assert session.id == "test-session-1"

    got = await crud.get_gen_session(db, "test-session-1")
    assert got is not None
    assert got.filename == "test.docx"


@pytest.mark.asyncio
async def test_get_session_not_found(db):
    assert await crud.get_gen_session(db, "nonexistent") is None


@pytest.mark.asyncio
async def test_list_gen_sessions(db):
    await crud.create_gen_session(db, "s1", "a.docx", '["a.docx"]', 1, "p1")
    await crud.create_gen_session(db, "s2", "b.docx", '["b.docx"]', 2, "p2")
    result = await crud.list_gen_sessions(db, page=1, page_size=10)
    assert result["total"] >= 2
    assert len(result["items"]) >= 2


@pytest.mark.asyncio
async def test_list_gen_sessions_filtered_by_project(db):
    await crud.create_gen_session(db, "s3", "c.docx", '["c.docx"]', 1, "p1")
    result = await crud.list_gen_sessions(db, page=1, page_size=10, project_id=1)
    assert all(s.project_id == 1 for s in result["items"])


@pytest.mark.asyncio
async def test_update_session_status(db):
    await crud.create_gen_session(db, "s-status", "f.docx", '["f.docx"]', 1, "p1")
    updated = await crud.update_gen_session_status(
        db, "s-status", status="completed", functional_points_count=5,
        test_cases_count=10,
    )
    assert updated.status == "completed"
    assert updated.functional_points_count == 5
    assert updated.test_cases_count == 10


@pytest.mark.asyncio
async def test_update_session_status_not_found(db):
    assert await crud.update_gen_session_status(db, "nonexistent", "done") is None


@pytest.mark.asyncio
async def test_delete_session(db):
    await crud.create_gen_session(db, "s-del", "d.docx", '["d.docx"]', 1, "p1")
    deleted = await crud.delete_gen_session(db, "s-del")
    assert deleted is not None
    assert await crud.get_gen_session(db, "s-del") is None


@pytest.mark.asyncio
async def test_delete_session_not_found(db):
    assert await crud.delete_gen_session(db, "nonexistent") is None


@pytest.mark.asyncio
async def test_list_functional_points_empty(db):
    fps = await crud.list_gen_functional_points(db, "no-session")
    assert fps == []


@pytest.mark.asyncio
async def test_list_test_cases_empty(db):
    tcs = await crud.list_gen_test_cases(db, "no-session")
    assert tcs == []
