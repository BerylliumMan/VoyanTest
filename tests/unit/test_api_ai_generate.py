"""040 US2：接口生成增强输入与流程草案校验。"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.gen.api import llm_cases


def _case(name="登录成功", steps=1):
    return {
        "name": name,
        "priority": "medium",
        "api_spec": {
            "schema_version": 1,
            "fail_policy": "fail_fast",
            "steps": [
                {
                    "order": i + 1,
                    "name": f"步骤{i + 1}",
                    "definition_id": 12 + i,
                    "request": {"method": "GET", "url": "{{baseUrl}}/x", "headers": [], "query": []},
                    "assertions": [{"type": "status_code", "condition": "equals", "expected": "200"}],
                }
                for i in range(steps)
            ],
        },
        "notes": ["n"],
    }


def _definition():
    return {
        "id": 12,
        "name": "登录",
        "method": "POST",
        "path": "/login",
        "summary": "用户登录",
        "request_schema": {
            "body": {"schema": {"type": "object"}, "example": {"username": "admin"}},
            "security": [{"name": "bearer", "type": "http", "scheme": "bearer"}],
        },
        "response_schema": {"statuses": {"200": {"example": {"token": "t"}}, "401": {"example": {}}}},
    }


@pytest.mark.asyncio
async def test_enhance_payload_includes_summary_security_and_modes(monkeypatch):
    captured = {}

    async def fake_resolve(*args, **kwargs):
        return ""

    async def fake_call(messages, **kwargs):
        captured["user"] = messages[-1]["content"]
        return json.dumps({"cases": [_case()]})

    monkeypatch.setattr("app.runtime_config.resolve_prompt_for_agent", fake_resolve)
    monkeypatch.setattr("app.gen.model_client.call_model", fake_call)
    cases, notes = await llm_cases.enhance_cases_with_llm(
        SimpleNamespace(), _definition(), [],
        {"use_llm": True, "security": True, "strong_assertions": True,
         "ai_modes": ["security", "strong_assertions"]},
    )
    assert len(cases) == 1
    payload = json.loads(captured["user"])
    assert payload["definition"]["summary"] == "用户登录"
    assert payload["definition"]["security"]
    assert payload["ai_modes"] == ["security", "strong_assertions"]


@pytest.mark.asyncio
async def test_flow_requires_two_steps(monkeypatch):
    async def fake_call(messages, **kwargs):
        return json.dumps({"cases": [_case(steps=1)]})

    monkeypatch.setattr("app.gen.model_client.call_model", fake_call)
    cases, notes = await llm_cases.enhance_flow_cases_with_llm(
        SimpleNamespace(), [_definition()], {"use_llm": True},
    )
    assert cases == []
    assert any("步骤少于 2 步" in n for n in notes)


@pytest.mark.asyncio
async def test_flow_accepts_multi_step(monkeypatch):
    async def fake_call(messages, **kwargs):
        return json.dumps({"cases": [_case("订单生命周期", steps=2)]})

    monkeypatch.setattr("app.gen.model_client.call_model", fake_call)
    cases, notes = await llm_cases.enhance_flow_cases_with_llm(
        SimpleNamespace(), [_definition()], {"use_llm": True},
    )
    assert len(cases) == 1
    assert cases[0]["source"] == "ai_flow"
    assert cases[0]["definition_ids"] == [12, 13]
