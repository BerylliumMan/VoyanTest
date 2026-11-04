"""047：变更即补用例（自动生成缺失用例）。"""

import json

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.models.api_test import ApiDefinition
from app.models.project import Project
from app.models.testcase import TestCase
from app.services.api_auto_cases import auto_generate_missing_cases

REQUEST_SCHEMA = {
    "params": [],
    "body": {
        "content_type": "application/json",
        "schema": {"type": "object", "properties": {"name": {"type": "string"}}},
        "example": {"name": "demo"},
    },
}


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
    project = Project(name="auto-case-demo")
    db.add(project)
    await db.commit()
    await db.refresh(project)
    return project.id


def _patch_settings(monkeypatch, **overrides):
    """pydantic Settings 字段不在类属性上，改为替换服务内的 get_settings。"""
    from types import SimpleNamespace

    from app.config import get_settings

    base = get_settings()
    values = {
        name: getattr(base, name)
        for name in dir(base)
        if not name.startswith("_") and not callable(getattr(base, name, None))
    }
    values.update(overrides)
    monkeypatch.setattr(
        "app.services.api_auto_cases.get_settings",
        lambda: SimpleNamespace(**values),
    )


async def _add_definition(db, project_id, path="/users", method="POST", **kw):
    request_schema = kw.pop("request_schema", REQUEST_SCHEMA)
    row = ApiDefinition(
        project_id=project_id,
        name=f"{method} {path}",
        method=method,
        path=path,
        source="spring",
        source_file="src/UserController.java",
        extractor="spring_source",
        status="active",
        request_schema=request_schema,
        **kw,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def _cases(db, project_id) -> list[TestCase]:
    return (
        await db.execute(select(TestCase).where(TestCase.project_id == project_id))
    ).scalars().all()


@pytest.mark.asyncio
async def test_generates_cases_for_definition_without_cases(db, project_id):
    definition = await _add_definition(db, project_id)

    stats = await auto_generate_missing_cases(
        db, project_id=project_id, definition_ids=[definition.id]
    )
    await db.commit()

    assert stats.generated_definitions == 1
    assert stats.generated_cases >= 1
    cases = await _cases(db, project_id)
    assert len(cases) == stats.generated_cases
    for case in cases:
        assert case.name.startswith("[自动]")
        assert case.case_kind == "api"
        assert case.api_definition_id == definition.id
        assert (case.api_spec or {}).get("meta", {}).get("auto_generated") is True
        assert case.api_spec.get("steps")

    await db.refresh(definition)
    assert definition.auto_case_generated_at is not None


@pytest.mark.asyncio
async def test_is_idempotent_per_definition(db, project_id):
    definition = await _add_definition(db, project_id)
    await auto_generate_missing_cases(
        db, project_id=project_id, definition_ids=[definition.id]
    )
    await db.commit()
    first = len(await _cases(db, project_id))

    stats = await auto_generate_missing_cases(
        db, project_id=project_id, definition_ids=[definition.id]
    )
    await db.commit()

    assert stats.generated_cases == 0
    assert len(await _cases(db, project_id)) == first


@pytest.mark.asyncio
async def test_disabled_switch_does_nothing(db, project_id, monkeypatch):
    _patch_settings(monkeypatch, api_auto_case_enabled=False)
    definition = await _add_definition(db, project_id)

    stats = await auto_generate_missing_cases(
        db, project_id=project_id, definition_ids=[definition.id]
    )
    await db.commit()

    assert stats.generated_cases == 0
    assert await _cases(db, project_id) == []


@pytest.mark.asyncio
async def test_max_definitions_per_run(db, project_id):
    defs = [await _add_definition(db, project_id, path=f"/p{i}") for i in range(6)]

    stats = await auto_generate_missing_cases(
        db, project_id=project_id, definition_ids=[d.id for d in defs]
    )
    await db.commit()

    assert stats.generated_definitions <= 5
    assert len(stats.skipped_definition_ids) >= 1


@pytest.mark.asyncio
async def test_max_cases_per_definition(db, project_id, monkeypatch):
    _patch_settings(monkeypatch, api_auto_case_max_each=1)
    definition = await _add_definition(db, project_id)

    stats = await auto_generate_missing_cases(
        db, project_id=project_id, definition_ids=[definition.id]
    )
    await db.commit()

    assert stats.generated_cases == 1
    assert len(await _cases(db, project_id)) == 1


@pytest.mark.asyncio
async def test_daily_limit(db, project_id, monkeypatch):
    _patch_settings(monkeypatch, api_auto_case_daily_limit=1)
    first = await _add_definition(db, project_id, path="/one")
    second = await _add_definition(db, project_id, path="/two")

    stats = await auto_generate_missing_cases(
        db, project_id=project_id, definition_ids=[first.id, second.id]
    )
    await db.commit()

    assert stats.generated_cases <= 1
    assert len(await _cases(db, project_id)) <= 1


@pytest.mark.asyncio
async def test_generator_error_does_not_block_others(db, project_id, monkeypatch):
    import app.gen.api.generator as gen

    good = await _add_definition(db, project_id, path="/good")
    bad = await _add_definition(db, project_id, path="/bad")
    original = gen.generate_cases_for_definition

    def flaky(definition, options=None, *, auth_definition=None):
        if definition.get("path") == "/bad":
            raise RuntimeError("boom")
        return original(definition, options, auth_definition=auth_definition)

    monkeypatch.setattr(gen, "generate_cases_for_definition", flaky)

    stats = await auto_generate_missing_cases(
        db, project_id=project_id, definition_ids=[bad.id, good.id]
    )
    await db.commit()

    assert stats.generated_cases >= 1
    assert any("boom" in e for e in stats.errors)
    rows = await _cases(db, project_id)
    assert all(c.api_definition_id == good.id for c in rows)


@pytest.mark.asyncio
async def test_manual_cases_untouched(db, project_id):
    definition = await _add_definition(db, project_id)
    manual = TestCase(
        project_id=project_id,
        name="人工用例",
        case_kind="api",
        api_definition_id=definition.id,
        api_spec={"schema_version": 1, "steps": []},
        status="active",
    )
    db.add(manual)
    await db.commit()
    await db.refresh(manual)

    await auto_generate_missing_cases(
        db, project_id=project_id, definition_ids=[definition.id]
    )
    await db.commit()

    await db.refresh(manual)
    assert manual.name == "人工用例"
    assert manual.status == "active"


AUTH_SCHEMA = {
    "params": [],
    "security": [{"type": "http", "scheme": "bearer"}],
    "body": {
        "content_type": "application/json",
        "schema": {"type": "object", "properties": {"name": {"type": "string"}}},
        "example": {"name": "demo"},
    },
}


async def _add_login_definition(db, project_id):
    row = ApiDefinition(
        project_id=project_id,
        name="登录",
        method="POST",
        path="/api/auth/login",
        source="openapi",
        extractor="openapi",
        status="active",
        request_schema={
            "body": {
                "content_type": "application/json",
                "schema": {"type": "object", "properties": {
                    "username": {"type": "string"}, "password": {"type": "string"}}},
                "example": {"username": "u", "password": "p"},
            }
        },
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


@pytest.mark.asyncio
async def test_auth_interface_gets_login_step(db, project_id):
    await _add_login_definition(db, project_id)
    definition = await _add_definition(
        db, project_id, path="/orders", method="GET", request_schema=AUTH_SCHEMA
    )

    stats = await auto_generate_missing_cases(
        db, project_id=project_id, definition_ids=[definition.id]
    )
    await db.commit()

    assert stats.generated_cases >= 1
    cases = await _cases(db, project_id)
    case = cases[0]
    steps = case.api_spec["steps"]
    assert len(steps) == 2
    assert steps[0]["order"] == 1
    assert steps[0]["request"]["url"].endswith("/api/auth/login")
    assert steps[0].get("extractors")
    auth_header = next(
        (h for h in steps[1]["request"].get("headers") or []
         if h.get("key", "").lower() == "authorization"),
        None,
    )
    assert auth_header and "{{token}}" in auth_header["value"]


@pytest.mark.asyncio
async def test_auth_interface_without_login_definition_has_note(db, project_id):
    definition = await _add_definition(
        db, project_id, path="/orders", method="GET", request_schema=AUTH_SCHEMA
    )

    stats = await auto_generate_missing_cases(
        db, project_id=project_id, definition_ids=[definition.id]
    )
    await db.commit()

    cases = await _cases(db, project_id)
    assert len(cases) == 1
    assert len(cases[0].api_spec["steps"]) == 1
    assert "token" in (cases[0].description or "").lower() or "token" in json.dumps(
        cases[0].api_spec, ensure_ascii=False
    ).lower()
