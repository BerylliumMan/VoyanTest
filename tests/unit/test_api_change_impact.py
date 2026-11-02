"""046：变更影响计算测试。"""

import pytest
import pytest_asyncio

from app.models.api_test import ApiDefinition
from app.models.project import Project
from app.models.testcase import TestCase
from app.services.api_change_impact import (
    compute_impact,
    match_definition_ids,
    normalize_repo_path,
)


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
    project = Project(name="impact-demo")
    db.add(project)
    await db.commit()
    await db.refresh(project)
    return project.id


def test_normalize_repo_path():
    assert normalize_repo_path("./src/A.java") == "src/A.java"
    assert normalize_repo_path("/src/A.java") == "src/A.java"
    assert normalize_repo_path("src\\A.java") == "src/A.java"
    assert normalize_repo_path(None) == ""


def test_match_definition_ids_by_source_file():
    hits = match_definition_ids(
        ["src/main/java/UserController.java", "README.md"],
        [
            (1, "src/main/java/UserController.java"),
            (2, "./src/main/java/OrderController.java"),
            (3, None),
        ],
    )
    assert hits == {1: ["src/main/java/UserController.java"]}


async def _add_definition(db, project_id, method="GET", path="/users", source_file="src/A.java"):
    row = ApiDefinition(
        project_id=project_id,
        name=f"{method} {path}",
        method=method,
        path=path,
        source="spring",
        source_file=source_file,
        extractor="spring_source",
        status="active",
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def _add_case(db, project_id, definition_id, name="接口用例", api_spec=None, status="active"):
    case = TestCase(
        project_id=project_id,
        name=name,
        case_kind="api",
        api_definition_id=definition_id,
        api_spec=api_spec,
        status=status,
    )
    db.add(case)
    await db.commit()
    await db.refresh(case)
    return case


@pytest.mark.asyncio
async def test_impact_links_cases_by_definition(db, project_id):
    definition = await _add_definition(db, project_id)
    case = await _add_case(db, project_id, definition.id)

    items = await compute_impact(
        db, project_id=project_id, changed_files=["src/A.java"]
    )
    assert len(items) == 1
    assert items[0].definition_id == definition.id
    assert items[0].case_ids == [case.id]
    assert items[0].method == "GET"
    assert items[0].path == "/users"


@pytest.mark.asyncio
async def test_impact_falls_back_to_api_spec_definition_id(db, project_id):
    definition = await _add_definition(db, project_id, "POST", "/orders", "src/Order.java")
    case = await _add_case(
        db,
        project_id,
        None,
        api_spec={"definition_id": definition.id, "steps": []},
    )

    items = await compute_impact(db, project_id=project_id, changed_files=["src/Order.java"])
    assert [c for item in items for c in item.case_ids] == [case.id]


@pytest.mark.asyncio
async def test_impact_skips_offline_definition(db, project_id):
    definition = await _add_definition(db, project_id)
    definition.status = "offline"
    await db.commit()
    await _add_case(db, project_id, definition.id)

    assert await compute_impact(db, project_id=project_id, changed_files=["src/A.java"]) == []


@pytest.mark.asyncio
async def test_impact_skips_unrelated_files(db, project_id):
    definition = await _add_definition(db, project_id)
    await _add_case(db, project_id, definition.id)

    assert await compute_impact(db, project_id=project_id, changed_files=["docs/readme.md"]) == []


@pytest.mark.asyncio
async def test_definition_without_cases_is_not_affected(db, project_id):
    await _add_definition(db, project_id)
    assert await compute_impact(db, project_id=project_id, changed_files=["src/A.java"]) == []


@pytest.mark.asyncio
async def test_inactive_case_excluded(db, project_id):
    definition = await _add_definition(db, project_id)
    await _add_case(db, project_id, definition.id, status="deprecated")

    assert await compute_impact(db, project_id=project_id, changed_files=["src/A.java"]) == []


@pytest.mark.asyncio
async def test_multiple_definitions_in_one_file(db, project_id):
    a = await _add_definition(db, project_id, "GET", "/a", "src/Same.java")
    b = await _add_definition(db, project_id, "POST", "/b", "src/Same.java")
    ca = await _add_case(db, project_id, a.id, "case-a")
    cb = await _add_case(db, project_id, b.id, "case-b")

    items = await compute_impact(db, project_id=project_id, changed_files=["src/Same.java"])
    got = {item.definition_id: item.case_ids for item in items}
    assert got == {a.id: [ca.id], b.id: [cb.id]}
