from app.gen.model_client import _extract_message_text


def test_extract_message_text_prefers_final_content_over_reasoning():
    assert _extract_message_text(
        {
            "content": '{"test_cases": []}',
            "reasoning_content": "内部推理",
        }
    ) == '{"test_cases": []}'


def test_extract_message_text_falls_back_to_reasoning_when_content_empty():
    assert _extract_message_text(
        {"content": "", "reasoning_content": '{"test_cases": []}'}
    ) == '{"test_cases": []}'
