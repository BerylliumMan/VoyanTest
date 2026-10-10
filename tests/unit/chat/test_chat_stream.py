"""034 流式输出：部分 JSON 提取 + 流式决策事件（thinking/reply 增量）。"""
import pytest

from app.gen import model_client as mc
from core.chat import orchestrator as orch
from core.chat import stream as chat_stream
from tests.unit.chat.conftest import FakeConversation


# ----------------------------
# 部分 JSON 提取（纯函数）
# ----------------------------

def test_extract_partial_string_basic_and_incomplete():
    buf = '{"thinking": "先看引用", "reply": "好的，我这就处理'
    assert chat_stream.extract_partial_string(buf, "thinking") == "先看引用"
    assert chat_stream.extract_partial_string(buf, "reply") == "好的，我这就处理"
    assert chat_stream.extract_partial_string(buf, "actions") == ""


def test_extract_partial_string_escapes_and_growth():
    b1 = '{"reply": "第一行\\n第二'
    assert chat_stream.extract_partial_string(b1, "reply") == "第一行\n第二"
    b2 = b1 + "行\\u4f"  # \uXXXX 不完整（仅 2 位十六进制）→ 前缀停在转义之前
    assert chat_stream.extract_partial_string(b2, "reply") == "第一行\n第二行"
    b3 = b2 + "60好"  # 补全转义后继续单调增长
    assert chat_stream.extract_partial_string(b3, "reply") == "第一行\n第二行你好"
    b4 = b3 + '\\", 完"'
    assert chat_stream.extract_partial_string(b4, "reply") == '第一行\n第二行你好", 完'


def test_partial_decision_stream_feed_is_monotonic():
    s = chat_stream.PartialDecisionStream()
    s.feed('{"thinking": "a')
    assert s.get("thinking") == "a" and s.get("reply") == ""
    s.feed('bc", "reply": "x')
    assert s.get("thinking") == "abc" and s.get("reply") == "x"
    s.feed('y"}')
    assert s.get("reply") == "xy"


# ----------------------------
# 流式决策调用（_stream_llm）
# ----------------------------

def _capture_emit(monkeypatch):
    events = []

    async def fake_emit(cid, ev):
        events.append(ev)

    monkeypatch.setattr(orch, "emit", fake_emit)
    return events


def _fake_streaming_call_model(reasoning_parts, content_parts, final_text):
    async def fake(messages, **kw):
        rc, cc = kw.get("reasoning_callback"), kw.get("stream_callback")
        for part in reasoning_parts:
            res = rc(part)
            if hasattr(res, "__await__"):
                await res
        for part in content_parts:
            res = cc(part)
            if hasattr(res, "__await__"):
                await res
        return final_text

    return fake


@pytest.mark.asyncio
async def test_stream_llm_prefers_reasoning_channel(chat_store, monkeypatch):
    conv = FakeConversation()
    events = _capture_emit(monkeypatch)
    monkeypatch.setattr(mc, "call_model", _fake_streaming_call_model(
        ["先看看证据", "，再决定动作"],
        ['{"thinking": "决策', '说明", "reply": "你好', '，世界"}'],
        '{"thinking": "决策说明", "reply": "你好，世界"}',
    ))

    raw = await orch._stream_llm(conv, [{"role": "user", "content": "hi"}], agent_id=None)
    assert "你好，世界" in raw

    kinds = [(e["type"], e.get("channel")) for e in events]
    assert ("message.stream_start", None) in kinds
    thinking = "".join(e["delta"] for e in events if e.get("channel") == "thinking")
    reply = "".join(e["delta"] for e in events if e.get("channel") == "reply")
    assert thinking == "先看看证据，再决定动作"  # 有 reasoning 时 thinking 用推理增量
    assert reply == "你好，世界"  # reply 从决策 JSON 增量中提取


@pytest.mark.asyncio
async def test_stream_llm_falls_back_to_json_thinking(chat_store, monkeypatch):
    conv = FakeConversation()
    events = _capture_emit(monkeypatch)
    monkeypatch.setattr(mc, "call_model", _fake_streaming_call_model(
        [],
        ['{"thinking": "先确认引', '用", "reply": "收到"}'],
        '{"thinking": "先确认引用", "reply": "收到"}',
    ))

    await orch._stream_llm(conv, [{"role": "user", "content": "hi"}], agent_id=None)
    thinking = "".join(e["delta"] for e in events if e.get("channel") == "thinking")
    assert thinking == "先确认引用"  # 无 reasoning → 用 JSON thinking 字段增量


@pytest.mark.asyncio
async def test_stream_llm_target_message_id_propagates(chat_store, monkeypatch):
    conv = FakeConversation()
    events = _capture_emit(monkeypatch)
    monkeypatch.setattr(mc, "call_model", _fake_streaming_call_model(
        ["r"], ['{"reply": "x"}'], '{"reply": "x"}',
    ))
    await orch._stream_llm(conv, [], agent_id=None, target_message_id=42)
    assert all(e.get("target_message_id") == 42 for e in events)


@pytest.mark.asyncio
async def test_handle_user_message_streams_and_stores_thinking(chat_store, monkeypatch):
    """生产路径（无 llm_call 注入）走流式：增量推送 + 最终消息含 thinking。"""
    conv = FakeConversation()
    events = _capture_emit(monkeypatch)
    monkeypatch.setattr(mc, "call_model", _fake_streaming_call_model(
        ["看意图"],
        ['{"thinking": "打招呼', '，引导", "reply": "你好', '！"}'],
        '{"thinking": "打招呼，引导", "reply": "你好！"}',
    ))

    _, assistant = await orch.handle_user_message(
        None, conversation=conv, user_id=7, content="你好",
    )
    assert assistant.content == "你好！" and assistant.thinking == "打招呼，引导"
    reply = "".join(e["delta"] for e in events if e.get("channel") == "reply")
    assert reply == "你好！"
    assert any(e["type"] == "message.stream_start" for e in events)
