"""040 US4：数据工厂校验与敏感列。"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.crud.api_dataset import DatasetValidationError, validate_dataset_meta


def test_meta_sensitive_columns_must_be_subset():
    assert validate_dataset_meta({"sensitive_columns": ["password"]}, ["password", "user"]) == {
        "sensitive_columns": ["password"]
    }
    with pytest.raises(DatasetValidationError):
        validate_dataset_meta({"sensitive_columns": ["missing"]}, ["user"])


def test_meta_rejects_bad_source_and_type():
    with pytest.raises(DatasetValidationError):
        validate_dataset_meta({"source": "magic"}, ["a"])
    with pytest.raises(DatasetValidationError):
        validate_dataset_meta({"sensitive_columns": "password"}, ["password"])


@pytest.mark.asyncio
async def test_generate_data_marks_sensitive_and_previews(monkeypatch):
    from app.services.api_ai_data import generate_data

    async def fake_resolve(*args, **kwargs):
        return ""

    async def fake_call(messages, **kwargs):
        return json.dumps({
            "columns": ["username", "password", "age"],
            "rows": [{"username": "u", "password": "p", "age": 1}],
            "expected": [{"row": 1, "expect": "200"}],
        })

    monkeypatch.setattr("app.runtime_config.resolve_prompt_for_agent", fake_resolve)
    monkeypatch.setattr("app.gen.model_client.call_model", fake_call)
    result = await generate_data(
        SimpleNamespace(),
        definition=SimpleNamespace(id=3, name="创建订单", method="POST", path="/x",
                                   request_schema={}, response_schema={}),
        project_id=1, save=False,
    )
    assert result["columns"] == ["username", "password", "age"]
    assert result["sensitive_columns"] == ["password"]
    assert result["meta"]["source"] == "ai"
    assert len(result["rows"]) == 1


@pytest.mark.asyncio
async def test_generate_data_rejects_bad_rows(monkeypatch):
    from app.services.api_ai_data import generate_data
    from app.services.api_ai import ApiAiUnavailable

    async def fake_call(messages, **kwargs):
        return json.dumps({"columns": ["a", "b"], "rows": [{"a": 1}]})

    monkeypatch.setattr("app.gen.model_client.call_model", fake_call)
    with pytest.raises(ApiAiUnavailable):
        await generate_data(
            SimpleNamespace(),
            definition=SimpleNamespace(id=3, name="x", method="GET", path="/x",
                                       request_schema={}, response_schema={}),
            project_id=1, save=False,
        )
