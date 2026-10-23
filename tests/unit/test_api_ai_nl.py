"""040 US3：自然语言候选检索与生成预览。"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
import pytest_asyncio

from app import db_models
from app.services.api_ai_generate import find_candidates, generate_from_text

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


async def _seed(db):
    project = db_models.Project(name="NL 项目")
    db.add(project)
    await db.flush()
    db.add_all([
        db_models.ApiDefinition(project_id=project.id, name="用户登录", method="POST", path="/api/login"),
        db_models.ApiDefinition(project_id=project.id, name="创建订单", method="POST", path="/api/orders"),
        db_models.ApiDefinition(project_id=project.id, name="订单列表", method="GET", path="/api/orders"),
    ])
    await db.commit()
    return project


@pytest.mark.asyncio
async def test_candidates_rank_by_name_and_path(db):
    project = await _seed(db)
    rows = await find_candidates(db, project.id, "创建订单后查询订单列表")
    assert [r.name for r in rows[:2]] == ["创建订单", "订单列表"]


@pytest.mark.asyncio
async def test_generate_preview_filters_unknown_ids(db, monkeypatch):
    project = await _seed(db)

    async def fake_resolve(*args, **kwargs):
        return ""

    async def fake_call(messages, **kwargs):
        return json.dumps({
            "summary": "s",
            "candidates": [{"definition_id": 1, "reason": "ok"}, {"definition_id": 999, "reason": "bad"}],
            "missing": ["缺少支付接口"],
            "scenario": {"name": "流程", "steps": [{"case_id": 1, "order": 1}], "variables": []},
        })

    monkeypatch.setattr("app.runtime_config.resolve_prompt_for_agent", fake_resolve)
    monkeypatch.setattr("app.gen.model_client.call_model", fake_call)
    result = await generate_from_text(
        db, project_id=project.id, text="下单流程", target="scenario",
    )
    assert result["target"] == "scenario"
    assert [c["definition_id"] for c in result["candidates"]] == [1]
    assert result["missing"] == ["缺少支付接口"]
    assert result["draft"]["name"] == "流程"


@pytest.mark.asyncio
async def test_generate_without_candidates(db):
    project = db_models.Project(name="空项目")
    db.add(project)
    await db.commit()
    result = await generate_from_text(db, project_id=project.id, text="任意", target="case")
    assert result["candidates"] == []
    assert result["missing"]
