"""048：登录接口查找公共服务测试。"""

from types import SimpleNamespace

import pytest
import pytest_asyncio

from app.services.api_case_auth import (
    find_login_definition,
    login_candidate,
    needs_auth,
)


def _row(**kw):
    base = {
        "id": 1,
        "name": "登录",
        "method": "POST",
        "path": "/api/auth/login",
        "request_schema": {"body": {"schema": {"type": "object",
                                                "properties": {"username": {"type": "string"},
                                                               "password": {"type": "string"}}}}},
    }
    base.update(kw)
    return SimpleNamespace(**base)


def test_needs_auth_reads_security():
    assert needs_auth({"request_schema": {"security": [{"type": "http"}]}}) is True
    assert needs_auth({"request_schema": {}}) is False


def test_login_candidate_accepts_password_post():
    candidate = login_candidate(_row())
    assert candidate is not None
    assert candidate["path"] == "/api/auth/login"


def test_login_candidate_rejects_non_post():
    assert login_candidate(_row(method="GET")) is None


def test_login_candidate_rejects_self_protected():
    row = _row(request_schema={
        "security": [{"type": "http"}],
        "body": {"schema": {"properties": {"password": {"type": "string"}}}},
    })
    assert login_candidate(row) is None


def test_login_candidate_by_path_keyword():
    row = _row(path="/v1/signin", request_schema={})
    assert login_candidate(row) is not None
    row2 = _row(path="/users", request_schema={})
    assert login_candidate(row2) is None


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
    from app.models.project import Project

    project = Project(name="auth-demo")
    db.add(project)
    await db.commit()
    await db.refresh(project)
    return project.id


@pytest.mark.asyncio
async def test_find_login_definition_prefers_password(db, project_id):
    from app.models.api_test import ApiDefinition

    db.add_all([
        ApiDefinition(project_id=project_id, name="登录", method="POST", path="/api/auth/login",
                      request_schema={"body": {"schema": {"properties": {
                          "username": {"type": "string"}, "password": {"type": "string"}}}}}),
        ApiDefinition(project_id=project_id, name="用户", method="GET", path="/users",
                      request_schema={}),
    ])
    await db.commit()

    found = await find_login_definition(db, project_id)
    assert found is not None
    assert found["path"] == "/api/auth/login"


@pytest.mark.asyncio
async def test_find_login_definition_none(db, project_id):
    from app.models.api_test import ApiDefinition

    db.add(ApiDefinition(project_id=project_id, name="用户", method="GET", path="/users",
                         request_schema={}))
    await db.commit()
    assert await find_login_definition(db, project_id) is None
