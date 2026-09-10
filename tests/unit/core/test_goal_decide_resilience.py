"""nl_goal 决策 LLM 的重试策略回归测试。

线上语料里 ``LLM API call failed after N attempts: Connection error.`` 是最大的
单一失败桶（数十个步骤）。原实现对**所有**异常统一处理：立刻重发且把
``Previous output invalid`` 追加进提示词 —— 传输层抖动时既没有退避，又污染了
下一轮提示词。现在按异常类型分流。
"""

from __future__ import annotations

import pytest

import core.goal_agent_loop as gal
from core.goal_agent_loop import decide_next_goal_action

VALID_DECISION = '{"status":"continue","thinking":"ok","action":"click","selector":"e12"}'


class _TransportError(Exception):
    """模拟 openai 的连接类异常（非 ValueError）。"""


class _Msg:
    def __init__(self, content: str) -> None:
        self.content = content


class _Resp:
    def __init__(self, content: str) -> None:
        self.choices = [type("_C", (), {"message": _Msg(content)})()]


class _ScriptedClient:
    """前 ``failures`` 次按 ``exc`` 抛错，之后返回 ``content``。"""

    def __init__(self, failures: int, content: str, exc: Exception) -> None:
        self.calls: list[dict] = []
        self._failures = failures
        self._content = content
        self._exc = exc
        self.chat = self
        self.completions = self

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if len(self.calls) <= self._failures:
            raise self._exc
        return _Resp(self._content)

    def hint_sent(self) -> bool:
        return any(
            "Previous output invalid" in m.get("content", "")
            for call in self.calls
            for m in call["messages"]
        )


async def _decide(client: _ScriptedClient):
    return await decide_next_goal_action(
        client=client,
        model="m",
        goal_text="GOAL",
        snapshot='- button "登录" [ref=e12]',
        journal_tail=[],
    )


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    """退避不能拖慢测试。"""

    async def _instant(_seconds):
        return None

    monkeypatch.setattr(gal.asyncio, "sleep", _instant)


@pytest.mark.asyncio
async def test_transport_error_retries_without_hint_pollution():
    client = _ScriptedClient(2, VALID_DECISION, _TransportError("Connection error."))

    decision = await _decide(client)

    assert decision.action == "click"
    assert len(client.calls) == 3
    # 关键：连接错误不能把「输出无效」的纠正提示塞进提示词
    assert client.hint_sent() is False


@pytest.mark.asyncio
async def test_transport_error_backs_off_between_attempts(monkeypatch):
    slept: list[float] = []

    async def _record(seconds):
        slept.append(seconds)

    monkeypatch.setattr(gal.asyncio, "sleep", _record)
    client = _ScriptedClient(2, VALID_DECISION, _TransportError("Connection error."))

    await _decide(client)

    # attempt=1 睡 1×base，attempt=2 睡 2×base（首次不睡）
    assert slept == [
        gal.GOAL_DECIDE_BACKOFF_SECONDS,
        gal.GOAL_DECIDE_BACKOFF_SECONDS * 2,
    ]


@pytest.mark.asyncio
async def test_invalid_output_gets_corrective_hint():
    client = _ScriptedClient(1, VALID_DECISION, ValueError("LLM did not return JSON: x"))

    decision = await _decide(client)

    assert decision.action == "click"
    assert client.hint_sent() is True


@pytest.mark.asyncio
async def test_transient_failure_recovers_within_one_turn():
    """单次瞬时抖动不应让整个用例失败（用例级失败桶的主要来源）。"""
    client = _ScriptedClient(1, VALID_DECISION, _TransportError("Connection error."))

    decision = await _decide(client)

    assert decision.selector == "e12"
    assert len(client.calls) == 2


@pytest.mark.asyncio
async def test_persistent_transport_failure_still_raises():
    client = _ScriptedClient(99, VALID_DECISION, _TransportError("Connection error."))

    with pytest.raises(ValueError) as exc:
        await _decide(client)

    assert "Connection error" in str(exc.value)
    assert len(client.calls) == gal.GOAL_DECIDE_ATTEMPTS
