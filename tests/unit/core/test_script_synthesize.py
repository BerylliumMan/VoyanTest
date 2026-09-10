"""Tests for core/script_synthesize.py Qwen thinking compatibility."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest


def _msg(content="", reasoning_content=""):
    return SimpleNamespace(content=content, reasoning_content=reasoning_content)


def test_synth_reads_reasoning_when_content_empty():
    from core.script_synthesize import _strip_fences
    from core.precondition import _response_text

    body = "```python\nasync def test_case_5(page):\n    pass\n```"
    assert "async def" in _strip_fences(_response_text(_msg("", body)))


def test_repair_unicode_lookalikes_fixes_arrow_and_fullwidth():
    from core.script_synthesize import (
        _accept_synthesized_script,
        repair_unicode_lookalikes,
    )

    broken = (
        'async def test_case_5(page):\n'
        '    await page.goto("https://x")  → 打开首页\n'
        '    await page.fill("a"， "b")\n'
    )
    fixed = repair_unicode_lookalikes(broken)
    assert "→" not in fixed and "，" not in fixed
    assert "async def" in _accept_synthesized_script(fixed, case_id=5)


def test_accept_rejects_unfixable_script():
    from core.script_synthesize import _accept_synthesized_script

    with pytest.raises(ValueError):
        _accept_synthesized_script("def broken(:\n  ???", case_id=5)


@pytest.mark.asyncio
async def test_synth_keeps_thinking_enabled_for_codegen():
    from core import script_synthesize as synth

    captured = {}
    body = "async def test_case_5(page):\n    pass"

    async def fake_create(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=_msg("", body))]
        )

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=fake_create))
    )
    with (
        patch.object(synth, "try_build_templated_script", return_value=None),
        patch.object(synth, "check_script_covers_intents", return_value=[]),
    ):
        script = await synth.synthesize_playwright_script(
            client=client,
            model="qwen",
            case_id=5,
            case_name="c",
            goal_text="g",
            journal=[],
            steps=[],
        )

    assert "async def test_case_5" in script
    assert "extra_body" not in captured
