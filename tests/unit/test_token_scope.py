"""T033 填充：令牌 scope 单测。

覆盖：
- 创建默认 read（不传 scope）
- 非法值拒绝（仅 read|write）
- NULL=write 兼容（历史令牌）
- read 令牌调 mutate 工具 → 拒绝文案（MCP 范围判定）
- auth 层 scope 传递（authenticate_api_token_full 返回 scope/项目范围）
"""

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.crud import api_token as crud_token
from app.models.api_test import ApiToken
from core.tools.registry import ToolSpec


@pytest_asyncio.fixture
async def db():
    """sqlite 内存库（users + api_tokens 表）。"""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.models.auth import User

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: User.__table__.create(c, checkfirst=True))
        await conn.run_sync(lambda c: ApiToken.__table__.create(c, checkfirst=True))
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as session:
        yield session
    await engine.dispose()


async def _make_user(db, username="tester"):
    from app.models.auth import User

    user = User(username=username, password_hash="x", role="admin", status="active")
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


@pytest.mark.asyncio
async def test_create_token_defaults_to_read(db):
    token, plaintext = await crud_token.create_token(db, name="默认范围")
    assert token.scope == "read"
    assert plaintext.startswith("vt_")
    public = crud_token.to_public_dict(token)
    assert public["scope"] == "read"


@pytest.mark.asyncio
async def test_create_token_explicit_write(db):
    token, _ = await crud_token.create_token(db, name="写范围", scope="write")
    assert token.scope == "write"


@pytest.mark.asyncio
async def test_create_token_invalid_scope_rejected(db):
    with pytest.raises(ValueError):
        await crud_token.create_token(db, name="坏范围", scope="admin")


@pytest.mark.asyncio
async def test_historical_null_scope_means_write(db):
    """历史令牌（scope NULL）语义 = 读写（CI 行为不变）。"""
    from app.auth import token_scope_allows_mutation

    token, plaintext = await crud_token.create_token(db, name="历史")
    # 模拟历史数据：置 NULL
    token.scope = None
    await db.commit()
    row = (await db.execute(select(ApiToken).where(ApiToken.token_hash == crud_token.hash_token(plaintext)))).scalar_one()
    assert row.scope is None
    assert token_scope_allows_mutation(row.scope) is True  # NULL → write
    public = crud_token.to_public_dict(row)
    assert public["scope"] == "write" and public.get("scope_legacy") is True


def test_scope_allows_mutation_matrix():
    from app.auth import token_scope_allows_mutation

    assert token_scope_allows_mutation("read") is False
    assert token_scope_allows_mutation("write") is True
    assert token_scope_allows_mutation(None) is True  # 会话/历史 → 全权


def _spec(side_effect: str) -> ToolSpec:
    async def h(ctx, **a):  # pragma: no cover - 不会被调用
        raise NotImplementedError

    return ToolSpec(
        name="t", description="d", parameters={"type": "object", "properties": {}},
        side_effect=side_effect, requires_confirmation=False, handler=h,
    )


def test_read_scope_blocks_mutate_tools():
    from app.routers.mcp.server import scope_allows_tool

    assert scope_allows_tool("read", _spec("read")) is True
    assert scope_allows_tool("read", _spec("mutate")) is False
    assert scope_allows_tool("read", _spec("execute")) is False
    assert scope_allows_tool("write", _spec("mutate")) is True
    assert scope_allows_tool(None, _spec("execute")) is True


@pytest.mark.asyncio
async def test_authenticate_full_returns_scope_and_project(db):
    from app.auth import authenticate_api_token_full

    user = await _make_user(db)
    token, plaintext = await crud_token.create_token(db, name="MCP", created_by=user.id, scope="read")
    user_row, row = await authenticate_api_token_full(db, f"Bearer {plaintext}")
    assert row is not None and row.id == token.id
    assert row.scope == "read"
    # 兼容函数仍返回二元组
    from app.auth import authenticate_api_token

    u2, project_scope = await authenticate_api_token(db, f"Bearer {plaintext}")
    assert u2 is not None and project_scope is None
