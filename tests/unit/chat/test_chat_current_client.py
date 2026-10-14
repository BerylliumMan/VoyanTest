# 034 迭代（2026-10-10）：对话默认选中「当前 PC 的客户端」——
#   按请求来源 IP 与在线客户端注册 IP 匹配；同机多客户端取最近活跃；
#   会话从未设置（agent_id=None）时自动绑定；显式选「服务端」存 0、不再被接管。
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.routers.chat.conversations import _pc_agent_for_request, match_pc_agent


def _agent(name: str, ip: str, seen=None, hostname: str = "pc"):
    return SimpleNamespace(name=name, ip_address=ip, hostname=hostname, last_seen=seen)


def test_match_requires_ip():
    assert match_pc_agent([_agent("a", "10.0.0.5")], "") is None
    assert match_pc_agent([], "10.0.0.5") is None


def test_match_prefers_recent_on_same_ip():
    older = _agent("old", "10.0.0.5", datetime(2026, 10, 10, 1, 0, tzinfo=timezone.utc))
    newer = _agent("new", "10.0.0.5", datetime(2026, 10, 10, 2, 0, tzinfo=timezone.utc))
    matched = match_pc_agent([older, newer], "10.0.0.5")
    assert matched is newer
    # 无 last_seen 不参与优先（但当唯一候选时仍可匹配）
    assert match_pc_agent([_agent("solo", "10.0.0.5")], "10.0.0.5").name == "solo"


def test_match_none_on_other_ip():
    assert match_pc_agent([_agent("a", "10.0.0.9")], "10.0.0.5") is None


def test_match_tolerates_missing_ip_field():
    matched = match_pc_agent([SimpleNamespace(name="x", last_seen=None)], "10.0.0.5")
    assert matched is None


@pytest.mark.asyncio
async def test_pc_agent_for_request_resolves_db_id(monkeypatch):
    import agent.manager as manager_mod

    online = [
        _agent("本机客户端-验证", "172.17.0.1",
               datetime(2026, 10, 10, 3, 0, tzinfo=timezone.utc)),
        _agent("other-pc", "192.168.1.50"),
    ]

    async def _fake_online():
        return online

    monkeypatch.setattr(manager_mod.agent_manager, "get_online_agents", _fake_online)

    async def _fake_get(db, name):
        assert name == "本机客户端-验证"
        return SimpleNamespace(id=42, name=name)

    monkeypatch.setattr("app.crud.agent.get_agent_by_name", _fake_get)

    request = SimpleNamespace(
        headers={"X-Forwarded-For": "172.17.0.1, 10.0.0.1"},
        client=SimpleNamespace(host="127.0.0.1"),
    )
    matched = await _pc_agent_for_request(None, request)
    assert matched == {"agent_id": 42, "agent_name": "本机客户端-验证", "ip": "172.17.0.1"}


@pytest.mark.asyncio
async def test_pc_agent_for_request_no_match(monkeypatch):
    import agent.manager as manager_mod

    async def _fake_online():
        return [_agent("other", "192.168.1.50")]

    monkeypatch.setattr(manager_mod.agent_manager, "get_online_agents", _fake_online)
    request = SimpleNamespace(headers={}, client=SimpleNamespace(host="172.17.0.1"))
    assert await _pc_agent_for_request(None, request) is None


@pytest.mark.asyncio
async def test_pc_agent_ws_only_client_id_zero(monkeypatch):
    """WS 在线但 DB 无行（极少见）→ agent_id=0（前端仅标注、不自动绑定）。"""
    import agent.manager as manager_mod

    async def _fake_online():
        return [_agent("ghost", "10.1.1.1")]

    monkeypatch.setattr(manager_mod.agent_manager, "get_online_agents", _fake_online)

    async def _fake_get(db, name):
        return None

    monkeypatch.setattr("app.crud.agent.get_agent_by_name", _fake_get)
    request = SimpleNamespace(headers={}, client=SimpleNamespace(host="10.1.1.1"))
    matched = await _pc_agent_for_request(None, request)
    assert matched == {"agent_id": 0, "agent_name": "ghost", "ip": "10.1.1.1"}
