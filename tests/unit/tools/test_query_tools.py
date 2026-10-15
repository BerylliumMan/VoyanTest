"""T022：只读查询工具单测。

契约行为：参数钳制 / 工具→服务调用透传 / 错误映射 / 六个只读工具注册与副作用标记。
（服务层的真实数据行为由 quickstart S2 在真实容器 + 真实 DB 实测覆盖。）
"""

import pytest

from app.services import tool_queries as tq
from core.tools.builtin import queries as q
from core.tools.registry import REGISTRY, ToolContext
from core.tools.result import ToolResult, error_result

SIX = ("list_projects", "search_cases", "get_case", "list_suites", "list_batches", "get_batch")


class TestClamp:
    def test_defaults(self):
        assert tq._clamp(None, None) == (20, 0)

    def test_bounds(self):
        assert tq._clamp(0, -5) == (1, 0)
        assert tq._clamp(1000, 3) == (100, 3)

    def test_strings(self):
        assert tq._clamp("7", "2") == (7, 2)

    def test_invalid(self):
        assert tq._clamp("x", "y") == (20, 0)


class TestRegistration:
    def test_six_tools_registered_as_read(self):
        names = set(REGISTRY.names())
        assert set(SIX) <= names
        for n in SIX:
            spec = REGISTRY.get(n)
            assert spec is not None
            assert spec.side_effect == "read"
            assert spec.parameters.get("type") == "object"


@pytest.mark.asyncio
async def test_list_projects_passthrough(monkeypatch):
    called = {}

    async def fake(db, user_id, *, query=None, limit=None, offset=None):
        called.update(db=db, user_id=user_id, query=query, limit=limit, offset=offset)
        return ToolResult(ok=True, summary="共 1 个项目", data={"items": [], "total": 1})

    monkeypatch.setattr(tq, "list_projects", fake)
    res = await q.list_projects(ToolContext(db="DB", user_id=7), query="demo", limit=5, offset=10)
    assert res.ok
    assert called == {"db": "DB", "user_id": 7, "query": "demo", "limit": 5, "offset": 10}


@pytest.mark.asyncio
async def test_search_cases_passthrough(monkeypatch):
    called = {}

    async def fake(db, user_id, *, project_id, query=None, case_kind=None, limit=None, offset=None):
        called.update(project_id=project_id, query=query, case_kind=case_kind)
        return ToolResult(ok=True, summary="ok")

    monkeypatch.setattr(tq, "search_cases", fake)
    await q.search_cases(ToolContext(db=None, user_id=1), project_id=24, query="登录", case_kind="ui")
    assert called == {"project_id": 24, "query": "登录", "case_kind": "ui"}


@pytest.mark.asyncio
async def test_get_case_error_mapping(monkeypatch):
    async def fake(db, user_id, *, case_id):
        return error_result(f"未找到用例 #{case_id}")

    monkeypatch.setattr(tq, "get_case", fake)
    res = await q.get_case(ToolContext(db=None, user_id=1), case_id=999)
    assert res.ok is False
    assert "未找到用例 #999" in res.error


@pytest.mark.asyncio
async def test_get_batch_passthrough(monkeypatch):
    async def fake(db, user_id, *, batch_id):
        return ToolResult(ok=True, summary=f"批次 #{batch_id} 状态 passed：1/1 通过")

    monkeypatch.setattr(tq, "get_batch", fake)
    res = await q.get_batch(ToolContext(db=None, user_id=1), batch_id=503)
    assert res.ok and "503" in res.summary
