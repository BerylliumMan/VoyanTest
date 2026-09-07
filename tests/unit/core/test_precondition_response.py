from types import SimpleNamespace

import pytest

from core.precondition import _response_text
from core.precondition import decide_precondition_action
from core.precondition import verify_precondition_met


def test_response_text_falls_back_to_reasoning_content_when_content_empty():
    message = SimpleNamespace(
        content="",
        reasoning_content='分析页面后决定执行：{"status":"continue","action":"wait","value":"2"}',
    )
    assert '"status":"continue"' in _response_text(message)


def test_response_text_prefers_content_when_model_returns_final_json():
    message = SimpleNamespace(
        content='{"status":"continue","action":"click","selector":"e12"}',
        reasoning_content="内部推理，不应作为动作解析",
    )
    assert _response_text(message) == message.content


@pytest.mark.asyncio
async def test_decide_precondition_action_accepts_reasoning_content_json():
    class Completions:
        async def create(self, **kwargs):
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content="",
                            reasoning_content=(
                                '分析完成：{"status":"continue",'
                                '"action":"wait","value":"2"}'
                            ),
                        )
                    )
                ]
            )

    client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
    result = await decide_precondition_action(
        client=client,
        model="Qwen3.8-Flash-Next",
        precondition="进入登录页",
        snapshot="页面加载中",
        journal_tail=[],
    )
    assert result.action == "wait"


@pytest.mark.asyncio
async def test_verify_precondition_met_accepts_reasoning_content_json():
    class Completions:
        async def create(self, **kwargs):
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content="",
                            reasoning_content=(
                                '结论：{"met":true,"reason":"登录页已打开"}'
                            ),
                        )
                    )
                ]
            )

    client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
    assert await verify_precondition_met(
        client=client,
        model="Qwen3.8-Flash-Next",
        snapshot="登录页",
        precondition="进入登录页",
    ) == (True, "登录页已打开")
