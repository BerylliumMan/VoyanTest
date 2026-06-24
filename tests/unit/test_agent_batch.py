"""Test agent batch execution — verifies sequential case processing after the duplicate-execution bug fix."""

import asyncio
import json
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

import pytest
from unittest.mock import patch, AsyncMock, MagicMock

from app.tz import now as tz_now
from agent.models import (
    AgentRegistration, WSMessage, WSMessageType,
    StepResultPayload, SnapshotPayload,
)
from agent.manager import agent_manager


async def _null_send(raw: str):
    pass


class MockRequestSession:
    """模拟 AgentSession，直接返回固定结果，不依赖 WebSocket 和浏览器。"""

    def __init__(self, agent_info, send_fn=_null_send):
        self.agent = agent_info
        self._send = send_fn
        self._pending = {}
        self._call_log = []  # 记录所有收到的消息

    async def send(self, msg: WSMessage):
        self._call_log.append(msg)

    async def request(self, msg: WSMessage) -> dict:
        self._call_log.append(msg)
        # 根据消息类型返回固定的 mock 响应
        if msg.type == WSMessageType.GET_SNAPSHOT:
            return {"text": '<html><body><button id="btn">mock</button></body></html>'}
        elif msg.type == WSMessageType.STEP_EXECUTE:
            return {
                "success": True,
                "action": f"{msg.payload.get('tool_call', {}).get('action', '?')}(done)",
                "error": None,
                "duration_ms": 10,
            }
        return {}

    def resolve(self, msg):
        key = msg.run_id
        fut = self._pending.pop(key, None)
        if fut and not fut.done():
            fut.set_result(msg.payload)


class _MockToolCall:
    """Mock return value for generate_tool_call."""
    def __init__(self):
        self.thinking = "mock thinking"
        self.next_goal = "done"
    def model_dump(self):
        return {
            "action": "click", "selector": "#btn", "value": None,
            "thinking": "mock thinking", "next_goal": "done",
        }

@pytest.fixture(autouse=True)
def mock_llm():
    """Mock generate_tool_call to avoid real LLM API calls in tests."""
    with patch("agent.manager.generate_tool_call",
               AsyncMock(return_value=_MockToolCall())):
        yield


@pytest.fixture(autouse=True)
def _mock_llm_config():
    """Mock LLM config/DB queries — agent batch tests don't need real LLM connections."""
    with patch('agent.manager._llm_resolve_config', new_callable=AsyncMock) as mock_resolve, \
         patch('agent.manager.create_openai_client', new_callable=AsyncMock) as mock_client:
        mock_resolve.return_value = ("sk-test", "https://api.test.com/v1", "test-model")
        mock_client.return_value = MagicMock()
        yield


@pytest.fixture
def registered_agent():
    """注册一个 mock agent 到 agent_manager，清理后注销。"""
    agent_id = "test-mock-agent"
    reg = AgentRegistration(
        name="MockAgent",
        hostname="test-host",
        ip_address="127.0.0.1",
        capabilities=["playwright", "ui_testing"],
    )
    session = MockRequestSession(agent_info=None)
    agent = agent_manager.register(agent_id, reg, session._send)
    session.agent = agent
    # 替换 session 为 mock
    agent_manager.sessions[agent_id] = session
    yield agent_id, session
    agent_manager.unregister(agent_id)


@pytest.mark.asyncio
async def test_execute_on_agent_single_case(registered_agent):
    """测试单用例执行：所有 step 正常完成。"""
    agent_id, session = registered_agent
    steps = [
        {"step_order": 1, "description": "打开首页"},
        {"step_order": 2, "description": "点击登录"},
    ]

    results = await agent_manager.execute_on_agent(agent_id, "run-001", "测试打开页面", steps)

    assert len(results) == 2
    assert results[0]["success"] is True
    assert results[1]["success"] is True
    assert results[0]["step_number"] == 1
    assert results[1]["step_number"] == 2

    # 验证发送了 RUN_START → GET_SNAPSHOT → STEP_EXECUTE(2次) → RUN_END
    types = [m.type for m in session._call_log]
    assert WSMessageType.RUN_START in types
    assert WSMessageType.RUN_END in types
    snapshot_count = types.count(WSMessageType.GET_SNAPSHOT)
    assert snapshot_count == 2, f"应有 2 次 GET_SNAPSHOT，实际 {snapshot_count}"


@pytest.mark.asyncio
async def test_execute_on_agent_sequential_cases(registered_agent):
    """测试顺序执行多个用例：连续调用 execute_on_agent 3 次。"""
    agent_id, session = registered_agent

    cases = [
        {"id": "run-001", "name": "用例A", "steps": [{"step_order": 1, "description": "打开A"}]},
        {"id": "run-002", "name": "用例B", "steps": [{"step_order": 1, "description": "打开B"}, {"step_order": 2, "description": "点击B"}]},
        {"id": "run-003", "name": "用例C", "steps": [{"step_order": 1, "description": "打开C"}]},
    ]

    for case in cases:
        session._call_log.clear()  # 每次调用后日志独立
        results = await agent_manager.execute_on_agent(agent_id, case["id"], case["name"], case["steps"])
        assert len(results) == len(case["steps"]), f"{case['name']}: 预期 {len(case['steps'])} 步，得到 {len(results)}"
        for r in results:
            assert r["success"] is True, f"{case['name']} 步骤 {r['step_number']} 失败"

    # 验证 agent 状态恢复为 ONLINE
    final_agent = agent_manager.get_online_agents()
    assert len(final_agent) == 1
    assert final_agent[0].status.value == "online"


@pytest.mark.asyncio
async def test_execute_on_agent_dedup(registered_agent):
    """验证每个 step 只执行一次（无重复执行 bug）。"""
    agent_id, session = registered_agent

    steps = [
        {"step_order": 1, "description": "第一步"},
        {"step_order": 2, "description": "第二步"},
    ]

    session._call_log.clear()
    await agent_manager.execute_on_agent(agent_id, "run-dedup", "去重测试", steps)

    # 统计 STEP_EXECUTE 消息数量
    execute_count = sum(1 for m in session._call_log if m.type == WSMessageType.STEP_EXECUTE)
    assert execute_count == len(steps), f"STEP_EXECUTE 应等于 step 数 ({len(steps)})，实际 {execute_count}"
