"""038 US5：生成并行测试夹具。"""
from __future__ import annotations

import asyncio

import pytest


class FakeCallModel:
    """可编排的 call_model 替身：记录调用/峰值并发，可注入延迟与异常。"""

    def __init__(self, *, delay: float = 0.0, result="[]", fail_times: int = 0):
        self.delay = delay
        self.result = result
        self.fail_times = fail_times
        self.calls: list[list] = []
        self.active = 0
        self.peak = 0

    async def __call__(self, messages, **kwargs):
        self.calls.append(messages)
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            if self.fail_times > 0:
                self.fail_times -= 1
                raise RuntimeError("429 rate limited")
            return self.result
        finally:
            self.active -= 1


@pytest.fixture
def fake_call_model():
    return FakeCallModel()
