"""040 US7：策略输入汇总（未覆盖/弱断言/失败历史）。"""
from __future__ import annotations

import pytest
import pytest_asyncio

from app import db_models
from app.services.api_ai_strategy import build_strategy_input


@pytest_asyncio.fixture
async def db():
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
    from app.database import Base
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as session:
        yield session
    await engine.dispose()


@pytest.mark.asyncio
async def test_strategy_input_flags_uncovered_weak_and_failed(db):
    project = db_models.Project(name="策略项目")
    db.add(project)
    await db.flush()
    covered = db_models.ApiDefinition(project_id=project.id, name="登录", method="POST", path="/login")
    uncovered = db_models.ApiDefinition(project_id=project.id, name="支付", method="POST", path="/pay")
    db.add_all([covered, uncovered])
    await db.flush()
    weak = db_models.TestCase(
        project_id=project.id, project_case_number=1, name="弱断言",
        case_kind="api", api_definition_id=covered.id,
        api_spec={"steps": [{"request": {"method": "POST", "url": "/login"},
                             "assertions": [{"type": "status_code", "condition": "equals", "expected": "200"}]}]},
    )
    strong = db_models.TestCase(
        project_id=project.id, project_case_number=2, name="强断言",
        case_kind="api", api_definition_id=covered.id,
        api_spec={"steps": [{"request": {"method": "POST", "url": "/login"},
                             "assertions": [
                                 {"type": "status_code", "condition": "equals", "expected": "200"},
                                 {"type": "jsonpath", "condition": "exists", "expression": "$.token"},
                             ]}]},
    )
    db.add_all([weak, strong])
    await db.flush()
    db.add(db_models.TestRun(case_id=weak.id, status="failed"))
    await db.commit()

    data = await build_strategy_input(db, project.id)
    assert data["uncovered_definitions"][0]["definition_id"] == uncovered.id
    assert [c["case_id"] for c in data["weak_assertion_cases"]] == [weak.id]
    assert data["recent_failed_case_ids"] == [weak.id]
