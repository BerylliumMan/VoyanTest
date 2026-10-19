"""034 US1：上下文组装与摘要（T011，先红）。"""
from types import SimpleNamespace

import pytest

from core.chat import context
from core.chat import orchestrator as orch
from core.chat.orchestrator import _active_draft_digest, _maybe_update_summary
from tests.unit.chat.conftest import FakeConversation, FakeMessage


def test_message_text_includes_cards_digest():
    m = FakeMessage(
        role="assistant", content="done",
        cards=[{"type": "draft_set", "case_kind": "ui", "items": [1, 2], "gen_session_id": "s1"}],
    )
    text = context.message_text(m)
    assert "done" in text and "草稿集" in text and "s1" in text


def test_split_for_summary_keeps_tail():
    msgs = [FakeMessage() for _ in range(15)]
    older, recent = context.split_for_summary(msgs, 12)
    assert len(older) == 3 and len(recent) == 12
    older2, recent2 = context.split_for_summary(msgs, 20)
    assert older2 == [] and len(recent2) == 15


def test_build_llm_messages_composition():
    msgs = [FakeMessage(role="user", content="hi")]
    out = context.build_llm_messages(
        system_prompt="SYS",
        summary="前情",
        messages=msgs,
        refs=[{"type": "case", "id": 5, "label": "登录用例"}],
        project_name="P1",
        draft_digest="【当前草稿集】2 条",
    )
    assert out[0]["role"] == "system"
    sys = out[0]["content"]
    assert "SYS" in sys and "【项目上下文】P1" in sys and "case#5" in sys
    assert "前情" in sys and "【当前草稿集】2 条" in sys
    assert out[1] == {"role": "user", "content": "hi"}


@pytest.mark.asyncio
async def test_summarize_history_llm_and_fallback():
    async def llm_ok(messages):
        return "摘要要点"

    assert await context.summarize_history(llm_ok, [FakeMessage(content="a")]) == "摘要要点"

    async def llm_bad(messages):
        raise RuntimeError("x")

    fb = await context.summarize_history(llm_bad, [FakeMessage(content="abc")])
    assert "abc" in fb


@pytest.mark.asyncio
async def test_summary_under_threshold_no_llm_call(chat_store):
    conv = FakeConversation()
    called = []

    async def llm(messages):
        called.append(1)
        return "SUM"

    summary, msgs = await _maybe_update_summary(None, conv, [FakeMessage(content="short")], llm)
    assert summary is None and len(msgs) == 1 and not called


@pytest.mark.asyncio
async def test_summary_over_threshold_incremental(chat_store, monkeypatch):
    monkeypatch.setattr(
        orch, "get_settings",
        lambda: SimpleNamespace(chat_context_max_chars=50, chat_summary_keep_messages=1),
    )
    # 2026-10-19：预算改由运行时配置优先（系统设置 → 对话与探索）
    monkeypatch.setattr(
        "app.runtime_config.chat_browse_config",
        SimpleNamespace(context_max_chars=50, context_keep_messages=1),
    )
    conv = FakeConversation()
    msgs = [
        FakeMessage(content="A" * 40),
        FakeMessage(content="B" * 40),
        FakeMessage(content="C" * 40),
    ]
    calls = []

    async def llm(messages):
        calls.append(messages)
        return "SUM1"

    summary, inject = await _maybe_update_summary(None, conv, msgs, llm)
    assert summary == "SUM1"
    assert len(inject) == 1 and inject[0].content.startswith("C")
    assert conv.summarized_until_message_id == msgs[1].id
    assert len(calls) == 1
    assert "AAAA" in calls[0][1]["content"] and "BBBB" in calls[0][1]["content"]  # 摘要输入含前两条

    # 增量：再追加两条 → 仅摘要 cutoff 之后、keep 之前的消息
    msgs2 = msgs + [FakeMessage(content="D" * 40), FakeMessage(content="E" * 40)]

    async def llm2(messages):
        calls.append(messages)
        return "SUM2"

    summary2, inject2 = await _maybe_update_summary(None, conv, msgs2, llm2)
    assert summary2 == "SUM1\nSUM2"
    assert inject2[-1].content.startswith("E")
    assert "CCCC" in calls[1][1]["content"] and "DDDD" in calls[1][1]["content"]
    assert "AAAA" not in calls[1][1]["content"]  # 已摘要的不重复送


def test_active_draft_digest_picks_latest_unimported():
    m1 = FakeMessage(role="assistant", cards=[
        {"type": "draft_set", "gen_session_id": "old", "case_kind": "ui", "status": "imported", "items": []},
    ])
    m2 = FakeMessage(role="assistant", cards=[
        {"type": "draft_set", "gen_session_id": "new", "case_kind": "functional", "status": "draft",
         "items": [{"draft_id": 1, "title": "登录成功"}, {"draft_id": 2, "title": "登录失败"}]},
    ])
    digest = _active_draft_digest([m1, m2])
    assert digest is not None and "new" in digest and "draft_id=1" in digest and "登录失败" in digest
    assert _active_draft_digest([m1]) is None  # 已导入的不算


@pytest.mark.asyncio
async def test_settings_block_functional_kind():
    """工作类型=功能用例：注入块说明手工语义（不可自动执行）。"""
    conv = FakeConversation(case_kind="functional")
    block = await orch._conversation_settings_block(None, conv)
    assert block is not None
    assert "功能用例" in block and "手工" in block and "run_case" in block


@pytest.mark.asyncio
async def test_settings_block_ui_default():
    conv = FakeConversation(case_kind="ui")
    block = await orch._conversation_settings_block(None, conv)
    assert "UI 自动化用例" in block


def test_compact_messages_truncates_when_over_budget():
    """2026-10-19：装配后超预算的确定性压缩（system 与最近消息优先保留）。"""
    from core.chat import context

    msgs = [
        {"role": "system", "content": "S" * 100},
        {"role": "user", "content": "A" * 5000},
        {"role": "assistant", "content": "B" * 5000},
        {"role": "user", "content": "C" * 5000},
    ]
    out = context.compact_messages(msgs, 8000)

    assert sum(len(m["content"]) for m in out) <= 8000
    assert "上下文自动压缩" in out[1]["content"]
    assert out[0]["content"] == "S" * 100  # system 不动
    assert msgs[1]["content"] == "A" * 5000  # 不改原对象
    # 未超限原样返回
    assert context.compact_messages(msgs, 100000) is msgs
