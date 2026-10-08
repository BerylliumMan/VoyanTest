"""PlaywrightMCPToolCall schema 容错：显式 null 取默认 + 思考模式响应回退。"""

import pytest

from core.llm_wrapper import PlaywrightMCPToolCall, generate_tool_call


class _FakeMessage:
    def __init__(self, content="", reasoning_content=None):
        self.content = content
        self.reasoning_content = reasoning_content
        self.model_extra = {}


class _FakeCompletions:
    def __init__(self, messages):
        self._messages = list(messages)
        self.calls = 0

    async def create(self, **kwargs):
        self.calls += 1
        message = self._messages[min(self.calls - 1, len(self._messages) - 1)]
        response = type("R", (), {})()
        response.choices = [type("C", (), {"message": message})()]
        return response


class _FakeClient:
    def __init__(self, messages):
        self.completions = _FakeCompletions(messages)
        self.chat = type("Chat", (), {"completions": self.completions})()


def test_explicit_null_fields_fall_back_to_defaults():
    tc = PlaywrightMCPToolCall.model_validate({
        "action": "done",
        "selector": None,
        "element_desc": None,
        "value": None,
        "timeout_ms": None,
        "needs_verification": None,
        "selector_type": None,
    })
    assert tc.timeout_ms == 30000
    assert tc.needs_verification is False
    assert tc.selector_type == "css"


@pytest.mark.asyncio
async def test_reasoning_content_json_is_used_without_retry(monkeypatch):
    async def _fake_resolve(**kwargs):
        return ("", "", "test-model")

    monkeypatch.setattr("core.llm_wrapper._resolve_config", _fake_resolve)
    client = _FakeClient([
        _FakeMessage(content="", reasoning_content='{"action":"wait","value":"2"}'),
    ])
    tc = await generate_tool_call("等待 2 秒", "", client=client)
    assert tc.action == "wait"
    assert client.completions.calls == 1


@pytest.mark.asyncio
async def test_plain_content_json_still_works(monkeypatch):
    async def _fake_resolve(**kwargs):
        return ("", "", "test-model")

    monkeypatch.setattr("core.llm_wrapper._resolve_config", _fake_resolve)
    client = _FakeClient([_FakeMessage(content='{"action":"click","selector":"e15"}')])
    tc = await generate_tool_call("点击登录", "", client=client)
    assert tc.action == "click"
    assert client.completions.calls == 1


# ── BATCH UNIT（Phase B）：unit 输出省略 action 的合法形态 ──────────────────
# 线上 run #334 根因回归：提示词允许 {"thinking", "unit": [...]}，而 schema 强制
# action → 每轮 3 次校验失败 → 「LLM 连续 4 轮未返回合法工具调用 JSON」。


def test_batch_unit_without_action_is_valid():
    tc = PlaywrightMCPToolCall.model_validate({
        "thinking": "登录三段机械动作",
        "unit": [
            {"action": "fill", "selector": "e11", "value": "standard_user"},
            {"action": "click", "selector": "e15"},
        ],
    })
    assert tc.action is None
    assert len(tc.unit) == 2
    assert tc.model_dump()["unit"][0]["action"] == "fill"


def test_action_and_unit_both_preserved():
    tc = PlaywrightMCPToolCall.model_validate({
        "action": "click",
        "selector": "e15",
        "unit": [{"action": "fill", "selector": "e11", "value": "u"}],
    })
    assert tc.action == "click"
    assert tc.model_dump()["unit"][0]["selector"] == "e11"


def test_missing_action_and_unit_rejected():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        PlaywrightMCPToolCall.model_validate({"thinking": "既没有 action 也没有 unit"})


@pytest.mark.asyncio
async def test_generate_tool_call_accepts_batch_unit(monkeypatch):
    async def _fake_resolve(**kwargs):
        return ("", "", "test-model")

    monkeypatch.setattr("core.llm_wrapper._resolve_config", _fake_resolve)
    client = _FakeClient([
        _FakeMessage(
            content='{"thinking":"batch","unit":[{"action":"click","selector":"e15"}]}'
        ),
    ])
    tc = await generate_tool_call("点击登录", "", client=client)
    assert tc.action is None
    assert len(tc.unit) == 1
    assert client.completions.calls == 1
