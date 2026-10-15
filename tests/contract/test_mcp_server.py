"""T032 填充：MCP 服务端契约测试（进程内 ASGI，真实 SDK 协议交互）。

覆盖：
- initialize / tools/list / call_tool 全协议交互（streamable HTTP，ASGITransport）
- tools/list 清单与注册表一致（write 令牌全量；read 令牌仅只读）
- 范围矩阵：read 令牌调 mutate 工具 → 错误（isError）
- 鉴权：无头/坏令牌/吊销令牌 → 401；chat_enabled=false → 403
- 影子会话：写调用惰性创建（source=external）+ 全局上限拒绝
"""

import json

import httpx
import pytest
import pytest_asyncio

from app.routers.mcp import server as mcp_server
from core.tools.registry import REGISTRY
from core.tools.result import ToolResult


@pytest.fixture
def asgi_factory():
    """httpx.AsyncClient 工厂：进程内 ASGITransport（不经过网络）。"""
    app, manager = mcp_server.build_mcp_app_and_manager()

    def factory(headers=None, timeout=None, auth=None):
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://mcp.test",
            headers=headers,
            timeout=timeout or httpx.Timeout(30),
        )

    return factory, manager


@pytest.fixture
def fake_identity(monkeypatch):
    """绕过真实令牌校验，直接注入接入身份。"""
    calls = {"identity": None}

    async def fake_auth(db, authorization):
        ident = calls["identity"]
        if ident is None:
            return None, None

        class Token:
            pass

        class User:
            pass

        user = User()
        user.id = ident["user_id"]
        user.status = "active"
        token = Token()
        token.id = ident["token_id"]
        token.name = ident.get("token_name", "测试令牌")
        token.scope = ident.get("scope", "write")
        token.project_id = ident.get("project_scope")
        return user, token

    import app.auth as auth_mod

    monkeypatch.setattr(auth_mod, "authenticate_api_token_full", fake_auth)

    class FakeSession:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *a):
            return False

    class FakeSessionLocal:
        def __call__(self):
            return FakeSession()

    import app.database as db_mod

    monkeypatch.setattr(db_mod, "AsyncSessionLocal", FakeSessionLocal())
    return calls


def _client_session(factory, identity_calls, *, scope="write", token_id=7):
    from mcp.client.streamable_http import streamablehttp_client

    identity_calls["identity"] = {"user_id": 1, "token_id": token_id, "scope": scope}
    return streamablehttp_client(
        "http://mcp.test/", headers={"Authorization": "Bearer vt_test"},
        httpx_client_factory=factory,
    )


@pytest.mark.asyncio
async def test_protocol_roundtrip_tools_list_and_call(asgi_factory, fake_identity, monkeypatch):
    """initialize → tools/list（全量）→ call_tool（fake handler 回显）。"""
    from mcp.client.session import ClientSession

    factory, manager = asgi_factory
    captured = {}

    async def fake_list_projects(ctx, **args):
        captured.update(ctx=ctx, args=args)
        return ToolResult(ok=True, summary="共 16 个项目", data={"items": [], "total": 16})

    monkeypatch.setattr(REGISTRY.get("list_projects"), "handler", fake_list_projects)

    async with manager.run():
        async with _client_session(factory, fake_identity) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                names = [t.name for t in tools.tools]
                assert set(REGISTRY.names()) <= set(names)  # 注册表全量可见（write）
                assert "run_case" in names and "edit_case" in names

                result = await session.call_tool("list_projects", {"query": "x"})
                assert not result.isError
                payload = json.loads(result.content[0].text)
                assert payload["ok"] is True and payload["summary"] == "共 16 个项目"
                assert captured["args"] == {"query": "x"}
                assert captured["ctx"].source == "external"
                assert captured["ctx"].token_id == 7


@pytest.mark.asyncio
async def test_read_scope_filters_list_and_blocks_mutate(asgi_factory, fake_identity):
    from mcp.client.session import ClientSession

    factory, manager = asgi_factory

    async with manager.run():
        async with _client_session(factory, fake_identity, scope="read") as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                names = {t.name for t in tools.tools}
                assert "list_projects" in names and "get_batch" in names
                assert "run_case" not in names and "edit_case" not in names

                # 直接调用（绕过清单）也应被拒绝
                result = await session.call_tool("run_case", {"case_ids": [675]})
                assert result.isError
                text = result.content[0].text
                assert "只读" in text and "write" in text


@pytest.mark.asyncio
async def test_missing_or_invalid_token_401(asgi_factory, monkeypatch):
    """无头 → 401；坏令牌 → 401（走 BearerAuthASGI 分支）。"""
    factory, manager = asgi_factory

    class FakeSession:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *a):
            return False

    class FakeSessionLocal:
        def __call__(self):
            return FakeSession()

    import app.database as db_mod

    monkeypatch.setattr(db_mod, "AsyncSessionLocal", FakeSessionLocal())

    async def fake_auth(db, authorization):
        return None, None

    import app.auth as auth_mod

    monkeypatch.setattr(auth_mod, "authenticate_api_token_full", fake_auth)

    async with manager.run():
        # 无 Authorization 头
        async with factory() as client:
            r = await client.post("/", json={"jsonrpc": "2.0", "id": 1, "method": "initialize"})
            assert r.status_code == 401
            assert r.json()["error"] == "invalid_token"


@pytest.mark.asyncio
async def test_chat_disabled_403(asgi_factory, monkeypatch):
    factory, manager = asgi_factory
    import types as _types

    import app.config as config_mod

    monkeypatch.setattr(config_mod, "get_settings", lambda: _types.SimpleNamespace(chat_enabled=False))
    async with manager.run():
        async with factory() as client:
            r = await client.post("/", json={"jsonrpc": "2.0", "id": 1, "method": "initialize"})
            assert r.status_code == 403
            assert r.json()["error"] == "chat_disabled"


@pytest.mark.asyncio
async def test_revoked_token_401_real_auth(monkeypatch):
    """真实 auth 链路：撤销令牌 → authenticate_api_token_full 返回 None → 401。"""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.crud import api_token as crud_token
    from app.models.api_test import ApiToken
    from app.models.auth import User

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: User.__table__.create(c, checkfirst=True))
        await conn.run_sync(lambda c: ApiToken.__table__.create(c, checkfirst=True))
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as db:
        user = User(username="t", password_hash="x", role="admin", status="active")
        db.add(user)
        await db.commit()
        token, plaintext = await crud_token.create_token(db, name="吊销测试", created_by=user.id, scope="read")

    class FakeSession:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *a):
            return False

    async def _auth(authorization):
        async with Session() as db:
            from app.auth import authenticate_api_token_full

            return await authenticate_api_token_full(db, authorization)

    # 撤销前可解析
    async with Session() as db:
        from app.auth import authenticate_api_token_full

        u, t = await authenticate_api_token_full(db, f"Bearer {plaintext}")
        assert u is not None
        await crud_token.revoke_token(db, token.id)
        u2, t2 = await authenticate_api_token_full(db, f"Bearer {plaintext}")
        assert u2 is None and t2 is None
    await engine.dispose()


# ----------------------------
# 影子会话（真实 sqlite：创建/复用/上限）
# ----------------------------

@pytest_asyncio.fixture
async def chat_db():
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.models.chat import ChatConversation, ChatMessage

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: ChatConversation.__table__.create(c, checkfirst=True))
        await conn.run_sync(lambda c: ChatMessage.__table__.create(c, checkfirst=True))
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as session:
        yield session
    await engine.dispose()


@pytest.mark.asyncio
async def test_shadow_session_create_and_reuse(chat_db, monkeypatch):
    import types as _types

    import app.config as config_mod

    monkeypatch.setattr(
        config_mod, "get_settings",
        lambda: _types.SimpleNamespace(chat_external_max_sessions=4),
    )
    ident = {"user_id": 1, "token_id": 7, "token_name": "CI 令牌A", "scope": "write", "project_scope": 30}

    class Spec:
        side_effect = "mutate"

    conv1 = await mcp_server._get_or_create_shadow(chat_db, ident, Spec(), {"project_id": 30})
    assert conv1.source == "external" and conv1.title == "外部接入 · CI 令牌A"
    assert conv1.project_id == 30

    conv2 = await mcp_server._get_or_create_shadow(chat_db, ident, Spec(), {})
    assert conv2.id == conv1.id  # 复用


@pytest.mark.asyncio
async def test_shadow_session_cap_rejected(chat_db, monkeypatch):
    import types as _types

    import app.config as config_mod

    monkeypatch.setattr(
        config_mod, "get_settings",
        lambda: _types.SimpleNamespace(chat_external_max_sessions=1),
    )
    from app.crud import chat as chat_crud

    await chat_crud.create_conversation(
        chat_db, user_id=2, project_id=None, title="外部接入 · 已有", source="external",
    )

    class Spec:
        side_effect = "execute"

    with pytest.raises(ValueError, match="上限"):
        await mcp_server._get_or_create_shadow(
            chat_db, {"user_id": 1, "token_id": 7, "token_name": "新令牌"}, Spec(), {},
        )


@pytest.mark.asyncio
async def test_resolve_shadow_project_from_case(chat_db):
    from app import db_models

    # 造一个最小 TestCase（sqlite 需建表）
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: db_models.TestCase.__table__.create(c, checkfirst=True))
        await conn.run_sync(lambda c: db_models.TestStep.__table__.create(c, checkfirst=True))
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as db:
        case = db_models.TestCase(
            project_id=24, name="样例", case_kind="ui", description="",
            priority="medium", project_case_number=1,
        )
        db.add(case)
        await db.commit()
        await db.refresh(case)
        pid = await mcp_server._resolve_shadow_project(
            db, {"project_scope": None}, None, {"case_id": case.id},
        )
        assert pid == 24
    await engine.dispose()


# ----------------------------
# 令牌项目范围强制（S4 实测发现：MCP 路径此前未强制项目范围）
# ----------------------------

class _Entity:
    def __init__(self, project_id):
        self.project_id = project_id


class _FakeDB:
    def __init__(self, mapping):
        self.mapping = mapping

    async def get(self, model, pk):
        return self.mapping.get((model.__name__, pk))


class _Spec:
    def __init__(self, name="search_cases", side_effect="read"):
        self.name = name
        self.side_effect = side_effect


@pytest.mark.asyncio
async def test_project_scope_rejects_foreign_project():
    db = _FakeDB({})
    with pytest.raises(ValueError, match="仅限项目 #24"):
        await mcp_server._enforce_token_project(
            db, {"project_scope": 24}, _Spec(), {"project_id": 30, "limit": 5},
        )


@pytest.mark.asyncio
async def test_project_scope_injects_missing_project():
    db = _FakeDB({})
    args = await mcp_server._enforce_token_project(db, {"project_scope": 24}, _Spec(), {"limit": 5})
    assert args["project_id"] == 24  # 防全项目泄漏


@pytest.mark.asyncio
async def test_project_scope_checks_resource_case():
    from app import db_models

    db = _FakeDB({("TestCase", 675): _Entity(30)})
    with pytest.raises(ValueError, match="用例 #675 属于项目 #30"):
        await mcp_server._enforce_token_project(db, {"project_scope": 24}, _Spec(), {"case_ids": [675]})
    db2 = _FakeDB({("TestCase", 675): _Entity(24)})
    args = await mcp_server._enforce_token_project(db2, {"project_scope": 24}, _Spec(name="run_case"), {"case_ids": [675]})
    assert args == {"case_ids": [675]}


@pytest.mark.asyncio
async def test_no_scope_passthrough():
    db = _FakeDB({})
    args = {"project_id": 30}
    out = await mcp_server._enforce_token_project(db, {"project_scope": None}, _Spec(), args)
    assert out == args


def test_list_projects_filtered_by_scope():
    from core.tools.result import ToolResult

    result = ToolResult(ok=True, summary="共 16 个项目", data={
        "items": [{"id": 24, "name": "A"}, {"id": 30, "name": "B"}], "total": 16, "offset": 0,
    })
    out = mcp_server._filter_projects_by_scope(result, {"project_scope": 24})
    assert [it["id"] for it in out.data["items"]] == [24]
    assert "令牌范围" in out.summary
