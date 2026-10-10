"""034 US5：失败归因 —— 证据组装 / 无证据固定话术（不调模型）/ grounded 解读（T044）。"""
import json

import pytest

from app.services import chat_evidence
from tests.unit.chat.conftest import FakeConversation


# ----------------------------
# 证据组装（纯逻辑）
# ----------------------------

def test_assemble_case_evidence_from_logs():
    logs = [
        {"level": "info", "message": "1. 打开登录页 — 成功"},
        {"level": "error", "message": "断言失败: 期望 equals【Products】，实际为【Swag Labs】 | [console] [ERROR] 404 layout.css"},
    ]
    ev = chat_evidence.build_case_evidence(
        case_id=662, case_title="登录后进入商品列表", logs=logs,
        steps_definition=[{"description": "点击【Login】", "expected": "进入商品页"}],
    )
    assert ev["has_evidence"] is True
    assert ev["step"].startswith("断言失败")
    assert ev["console"] and ev["console"][0].startswith("[console]")
    assert ev["steps_definition"][0]["description"] == "点击【Login】"


def test_assemble_case_evidence_empty_is_insufficient():
    ev = chat_evidence.build_case_evidence(case_id=1, case_title="T", logs=[], steps_definition=[])
    assert ev["has_evidence"] is False
    assert ev["step"] is None


def test_format_insufficient_reply_is_fixed_text():
    text = chat_evidence.insufficient_reply(batch_id=99)
    assert "证据不足" in text and "#99" in text


def test_parse_explanation_valid_and_invalid():
    raw = json.dumps({
        "analysis": "断言与页面标题不匹配",
        "grounded_on": ["失败步骤文本", "console 404"],
        "suggestions": [
            {"title": "修正断言文案", "reason": "页面标题为 Swag Labs", "apply_prompt": "把第 2 步断言改成断言页面标题为 Swag Labs"},
        ],
    })
    parsed, err = chat_evidence.parse_explanation(raw)
    assert err == "" and parsed["analysis"].startswith("断言")
    assert len(parsed["suggestions"]) == 1 and parsed["suggestions"][0]["apply_prompt"]

    assert chat_evidence.parse_explanation("not json")[0] is None
    assert chat_evidence.parse_explanation(json.dumps({"analysis": ""}))[0] is None


# ----------------------------
# explain 服务
# ----------------------------

@pytest.mark.asyncio
async def test_explain_without_evidence_never_calls_llm(monkeypatch):
    conv = FakeConversation(project_id=30)
    calls = []

    async def fake_assemble(db, *, batch_id, case_id=None):
        return {"batch_id": batch_id, "case_id": case_id, "has_evidence": False, "cases": []}

    async def llm(messages):
        calls.append(1)
        return "{}"

    monkeypatch.setattr(chat_evidence, "assemble_evidence", fake_assemble)
    card, err = await chat_evidence.explain(
        None, conversation=conv, batch_id=99, case_id=None, llm_call=llm,
    )
    assert err == "" and card["status"] == "insufficient"
    assert "证据不足" in card["analysis"]
    assert calls == []  # 红线：无证据不调用模型


@pytest.mark.asyncio
async def test_explain_with_evidence_llm_success(monkeypatch):
    conv = FakeConversation(project_id=30)

    async def fake_assemble(db, *, batch_id, case_id=None):
        return {
            "batch_id": batch_id, "case_id": case_id, "has_evidence": True,
            "cases": [{
                "case_id": 662, "case_title": "T", "step": "断言失败: …",
                "console": ["[console] [ERROR] 404"], "screenshot_url": None,
                "failed_count": 1, "steps_definition": [{"description": "断言包含 Products", "expected": ""}],
            }],
        }

    async def llm(messages):
        return json.dumps({
            "analysis": "断言文案与实际页面不符",
            "grounded_on": ["失败步骤：断言失败"],
            "suggestions": [{"title": "修正断言", "reason": "见失败步骤", "apply_prompt": "把该步断言改为断言页面包含 Swag Labs"}],
        })

    monkeypatch.setattr(chat_evidence, "assemble_evidence", fake_assemble)
    card, err = await chat_evidence.explain(
        None, conversation=conv, batch_id=456, case_id=662, llm_call=llm,
    )
    assert err == "" and card["status"] == "ready"
    assert card["analysis"].startswith("断言文案")
    assert card["grounded_on"] and card["suggestions"][0]["apply_prompt"]
    assert card["evidence"]["cases"][0]["console"]


@pytest.mark.asyncio
async def test_explain_llm_failure_falls_back_to_evidence_only(monkeypatch):
    conv = FakeConversation(project_id=30)

    async def fake_assemble(db, *, batch_id, case_id=None):
        return {
            "batch_id": batch_id, "case_id": case_id, "has_evidence": True,
            "cases": [{"case_id": 662, "case_title": "T", "step": "断言失败: …",
                       "console": [], "screenshot_url": None, "failed_count": 1, "steps_definition": []}],
        }

    async def llm(messages):
        raise RuntimeError("gateway down")

    monkeypatch.setattr(chat_evidence, "assemble_evidence", fake_assemble)
    card, err = await chat_evidence.explain(
        None, conversation=conv, batch_id=456, case_id=None, llm_call=llm,
    )
    assert err == "" and card["status"] == "ready"
    assert "模型解读失败" in card["analysis"]
    assert card["suggestions"] == []
    assert card["evidence"]["cases"]


@pytest.mark.asyncio
async def test_explain_handler_registers_card(monkeypatch):
    conv = FakeConversation(project_id=30)

    async def fake_explain(db, *, conversation, batch_id, case_id, llm_call=None):
        return {"type": "evidence", "batch_id": batch_id, "status": "ready",
                "analysis": "A", "grounded_on": [], "suggestions": [], "evidence": {}}, ""

    monkeypatch.setattr(chat_evidence, "explain", fake_explain)
    outcome = await chat_evidence.explain_handler(
        None, conv, {"type": "explain", "batch_id": 456, "case_id": 662},
        {"user_id": 7, "llm": None},
    )
    assert outcome.ok and outcome.card["type"] == "evidence" and outcome.card["batch_id"] == 456
