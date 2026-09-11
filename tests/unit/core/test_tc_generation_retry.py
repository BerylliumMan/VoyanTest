import pytest

from app.gen.feature_extractor import _generate_batch_once
from app.gen.models import FunctionalPoint


@pytest.mark.asyncio
async def test_empty_tc_batch_retry_forbids_empty_array(monkeypatch):
    prompts = []
    user_messages = []
    call_options = []

    async def fake_call_model(messages, **kwargs):
        prompts.append(messages[0]["content"])
        user_messages.append(messages[1]["content"])
        call_options.append(kwargs)
        if len(prompts) == 1:
            return "[]"
        return '[{"module":"订单","title":"查询订单","test_steps":["打开查询页"],"expected_result":["页面打开"]}]'

    monkeypatch.setattr("app.gen.feature_extractor.call_model", fake_call_model)
    result = await _generate_batch_once(
        batch=[FunctionalPoint(module="订单", name="查询订单")],
        tc_prompt="输出 JSON 测试用例",
        project_description="订单系统",
        agent_type="generation",
        agent_id=None,
        tc_counter=0,
        user_hint="为测试项生成用例",
    )

    assert len(result) == 1
    assert len(prompts) == 2
    assert "previous response was an empty array" in prompts[1]
    assert "上一阶段从需求正文提取的权威素材" in user_messages[0]
    assert "附图" not in user_messages[0]
    assert all(options["enable_thinking"] is None for options in call_options)
