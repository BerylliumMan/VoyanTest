"""044：LLM 调用节流按 Agent/用户隔离。

原实现是模块级 `_last_call_ts: float` + 固定 3s 间隔，所有用户/Agent 共用一个时间戳，
多用户并发对话会互相阻塞。新实现按 key 维护独立时间戳，并把默认间隔降到 250ms。
"""

import asyncio

import pytest

from app.gen import model_client


@pytest.mark.asyncio
async def test_pacing_is_isolated_per_key(monkeypatch):
    monkeypatch.setattr(model_client, "_LLM_MIN_INTERVAL", 0.2)
    model_client._last_call_ts.clear()
    t0 = asyncio.get_event_loop().time()
    await model_client._pace_calls("agent:1")
    await model_client._pace_calls("agent:2")  # 不同 key 不应等待
    elapsed = asyncio.get_event_loop().time() - t0
    assert elapsed < 0.15


@pytest.mark.asyncio
async def test_pacing_waits_within_same_key(monkeypatch):
    monkeypatch.setattr(model_client, "_LLM_MIN_INTERVAL", 0.2)
    model_client._last_call_ts.clear()
    await model_client._pace_calls("agent:1")
    t0 = asyncio.get_event_loop().time()
    await model_client._pace_calls("agent:1")
    assert asyncio.get_event_loop().time() - t0 >= 0.15
