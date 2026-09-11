"""Tests for P5 — model_client.call_model 对 5xx/网络错误退避重试。

契约: 4xx 不重试（429 限速除外）；5xx/429/TimeoutException/TransportError 最多 5 次尝试。
"""
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from app.gen import model_client


@pytest.fixture(autouse=True)
def _no_pacing(monkeypatch):
    monkeypatch.setattr(model_client, "_LLM_MIN_INTERVAL", 0)
    monkeypatch.setattr(model_client, "_last_call_ts", 0.0)


class FakeResp:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload or {
            "choices": [{"finish_reason": "stop",
                         "message": {"content": "hello"}}]
        }
        self.request = httpx.Request("POST", "http://x")

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}", request=self.request, response=self)

    def json(self):
        return self._payload


class FakeClient:
    """按脚本依次返回响应；记录调用次数。"""

    script: list = []
    calls: int = 0

    def __init__(self, *a, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, *a, **kw):
        idx = min(FakeClient.calls, len(FakeClient.script) - 1)
        FakeClient.calls += 1
        item = FakeClient.script[idx]
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture(autouse=True)
def _patch(monkeypatch):
    async def _no_sleep(*a, **kw):
        return None
    monkeypatch.setattr(model_client.asyncio, "sleep", _no_sleep)

    async def _cfg(**kw):
        return {"api_base": "http://x/v1", "model": "m", "api_key": "k",
                "temperature": 0.1, "max_context_tokens": 98304}
    monkeypatch.setattr(model_client, "_load_ai_config", _cfg)
    yield


@pytest.mark.asyncio
async def test_retry_on_500_then_success(monkeypatch):
    FakeClient.script = [FakeResp(500), FakeResp(502), FakeResp(200)]
    monkeypatch.setattr(model_client.httpx, "AsyncClient", FakeClient)
    FakeClient.calls = 0
    out = await model_client.call_model([{"role": "user", "content": "hi"}])
    assert out == "hello"
    assert FakeClient.calls == 3


@pytest.mark.asyncio
async def test_no_retry_on_400():
    FakeClient.script = [FakeResp(400)]
    monkeypatch_mod = model_client.httpx
    orig = monkeypatch_mod.AsyncClient
    monkeypatch_mod.AsyncClient = FakeClient
    FakeClient.calls = 0
    with pytest.raises(httpx.HTTPStatusError):
        await model_client.call_model([{"role": "user", "content": "hi"}])
    assert FakeClient.calls == 1
    monkeypatch_mod.AsyncClient = orig


@pytest.mark.asyncio
async def test_retry_exhausted_raises(monkeypatch):
    FakeClient.script = [FakeResp(500)] * 5
    monkeypatch.setattr(model_client.httpx, "AsyncClient", FakeClient)
    FakeClient.calls = 0
    with pytest.raises(httpx.HTTPStatusError):
        await model_client.call_model([{"role": "user", "content": "hi"}])
    assert FakeClient.calls == 5


@pytest.mark.asyncio
async def test_retry_on_timeout_exception(monkeypatch):
    FakeClient.script = [httpx.ReadTimeout("t"), FakeResp(200)]
    monkeypatch.setattr(model_client.httpx, "AsyncClient", FakeClient)
    FakeClient.calls = 0
    out = await model_client.call_model([{"role": "user", "content": "hi"}])
    assert out == "hello"
    assert FakeClient.calls == 2


# ── 修复 2：stream 路径同样重试 ─────────────────────────────────────────────


class FakeStream:
    """模拟 httpx stream 响应（含 500/200 两种状态与 SSE 行）。"""

    def __init__(self, status_code):
        self.status_code = status_code
        self.request = httpx.Request("POST", "http://x")

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}", request=self.request, response=self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def aiter_lines(self):
        if self.status_code == 200:
            yield 'data: {"choices":[{"delta":{"content":"he"}}]}'
            yield 'data: {"choices":[{"delta":{"content":"llo"}}]}'
            yield "data: [DONE]"
        else:
            yield ""


class FakeStreamClient(FakeClient):
    """FakeClient 的 stream 变体：script 元素为 (status, lines)。"""

    def stream(self, *a, **kw):
        idx = min(FakeStreamClient.calls, len(FakeStreamClient.script) - 1)
        FakeStreamClient.calls += 1
        status = FakeStreamClient.script[idx]
        return FakeStream(status)


@pytest.mark.asyncio
async def test_stream_retry_on_500_then_success(monkeypatch):
    """stream 模式 500→200 重试成功，流内容拼接正确。"""
    FakeStreamClient.script = [500, 200]
    monkeypatch.setattr(model_client.httpx, "AsyncClient", FakeStreamClient)
    FakeStreamClient.calls = 0
    chunks: list[str] = []
    out = await model_client.call_model(
        [{"role": "user", "content": "hi"}],
        stream_callback=chunks.append,
    )
    assert out == "hello"
    assert "".join(chunks) == "hello"
    assert FakeStreamClient.calls == 2


@pytest.mark.asyncio
async def test_stream_retry_exhausted_raises(monkeypatch):
    """stream 模式连续 500 耗尽后抛出。"""
    FakeStreamClient.script = [500, 500, 500]
    monkeypatch.setattr(model_client.httpx, "AsyncClient", FakeStreamClient)
    FakeStreamClient.calls = 0
    with pytest.raises(httpx.HTTPStatusError):
        await model_client.call_model(
            [{"role": "user", "content": "hi"}], stream_callback=lambda _: None)
    assert FakeStreamClient.calls == 5


def test_429_is_retryable_with_retry_after():
    """429 可重试；服从 Retry-After（上限 120s），无头则指数退避。"""
    from app.gen.model_client import _is_retryable, _retry_delay

    req = httpx.Request("POST", "http://x")

    def err_429(headers=None):
        resp = httpx.Response(429, headers=headers or {}, request=req)
        return httpx.HTTPStatusError("429", request=req, response=resp)

    assert _is_retryable(err_429()) is True
    assert _retry_delay(err_429(), 1) == 6.0
    assert _retry_delay(err_429({"retry-after": "30"}), 1) == 30.0
    assert _retry_delay(err_429({"retry-after": "999"}), 1) == 120.0

    resp400 = httpx.Response(400, request=req)
    assert _is_retryable(httpx.HTTPStatusError("400", request=req, response=resp400)) is False
