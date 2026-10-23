"""040 US2：生成提示词必须包含示例/安全/多步骤/强断言约束。"""
from app.gen.api.prompts import (
    API_CASE_GENERATE_PROMPT,
    API_CONTRACT_PROMPT,
    API_DATA_FACTORY_PROMPT,
    API_FAILURE_ANALYSIS_PROMPT,
    API_FLOW_GENERATE_PROMPT,
    API_NL_GENERATE_PROMPT,
    API_STRATEGY_PROMPT,
)


def test_case_prompt_has_new_inputs_and_modes():
    for needle in ("description", "security", "ai_modes", "strong_assertions", "示例值"):
        assert needle in API_CASE_GENERATE_PROMPT




def test_flow_prompt_requires_cross_definition_steps_and_variables():
    assert "definition_id" in API_FLOW_GENERATE_PROMPT
    assert "extractors" in API_FLOW_GENERATE_PROMPT
    assert "{{变量}}" in API_FLOW_GENERATE_PROMPT
    assert "condition 只允许" in API_FLOW_GENERATE_PROMPT


def test_other_prompts_are_non_empty_and_structured():
    for prompt in (API_FAILURE_ANALYSIS_PROMPT, API_NL_GENERATE_PROMPT,
                   API_DATA_FACTORY_PROMPT, API_CONTRACT_PROMPT, API_STRATEGY_PROMPT):
        assert len(prompt) > 80
        assert "{" in prompt
