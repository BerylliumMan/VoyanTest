"""Unit-test fixtures for core engine tests (no external services)."""

import json
from pathlib import Path

import pytest
import pytest_asyncio


@pytest_asyncio.fixture
async def db():
    """Real SQLite session satisfying FK constraints for orchestrator tests."""
    from sqlalchemy.ext.asyncio import (
        AsyncSession,
        async_sessionmaker,
        create_async_engine,
    )

    from app.database import Base

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as session:
        yield session
    await engine.dispose()

# ── 029-api-testing：接口文档解析 fixture 读取 ──────────────────────────────

_API_DOC_FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "api_doc"


@pytest.fixture
def api_doc_fixture():
    """读取 tests/fixtures/api_doc/<name> 的原始字节与解析后的 JSON。"""

    def _load(name: str):
        raw = (_API_DOC_FIXTURES / name).read_bytes()
        return raw, json.loads(raw.decode("utf-8"))

    return _load


# ── 032-exec-evidence：执行链路测试桩 ─────────────────────────────────────────


class FakeMCPCaller:
    """记录 MCP 调用并返回可编排结果的调用桩（签名兼容 browser_probe 的 caller）。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.responses: dict[str, list] = {}  # tool -> 依次返回（最后一项会复用）
        self.default: dict = {"success": True, "text": ""}

    def set(self, tool: str, *responses) -> None:
        self.responses[tool] = list(responses)

    async def __call__(self, tool: str, args: dict) -> dict:
        self.calls.append((tool, args))
        queue = self.responses.get(tool)
        if queue:
            item = queue.pop(0) if len(queue) > 1 else queue[0]
            if isinstance(item, Exception):
                raise item
            return item
        return dict(self.default)


class FakeAgentManager:
    """send_observe / send_act 可编程的管理器桩（可模拟超时/中断/回包序列）。"""

    def __init__(self) -> None:
        self.act_calls: list[dict] = []
        self.observe_calls: list[dict] = []
        self.act_results: list = []
        self.observe_results: list = []
        self.default_act: dict = {"success": True, "text": ""}
        self.default_observe: dict = {"snapshot": "", "page_url": ""}

    def queue_act(self, result) -> None:
        self.act_results.append(result)

    def queue_observe(self, result) -> None:
        self.observe_results.append(result)

    async def send_act(self, agent_id, run_id, action, **kwargs):
        self.act_calls.append({"agent_id": agent_id, "run_id": run_id, "action": action, **kwargs})
        result = self.act_results.pop(0) if self.act_results else dict(self.default_act)
        if isinstance(result, Exception):
            raise result
        return result

    async def send_observe(self, agent_id, run_id, **kwargs):
        self.observe_calls.append({"agent_id": agent_id, "run_id": run_id, **kwargs})
        result = self.observe_results.pop(0) if self.observe_results else dict(self.default_observe)
        if isinstance(result, Exception):
            raise result
        return result


@pytest.fixture
def fake_mcp_caller() -> FakeMCPCaller:
    return FakeMCPCaller()


@pytest.fixture
def fake_agent_manager() -> FakeAgentManager:
    return FakeAgentManager()


@pytest.fixture
def execution_control_state():
    """038 US3：隔离 execution_control 进程内标志（测试后恢复）。"""
    from app import execution_control as ec

    saved_stopped = set(ec._stopped)
    saved_paused = set(ec._paused)
    ec._stopped.clear()
    ec._paused.clear()
    try:
        yield ec
    finally:
        ec._stopped.clear()
        ec._stopped.update(saved_stopped)
        ec._paused.clear()
        ec._paused.update(saved_paused)
