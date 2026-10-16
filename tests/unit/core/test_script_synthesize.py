"""Tests for core/script_synthesize.py Qwen thinking compatibility."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest


def _msg(content="", reasoning_content=""):
    return SimpleNamespace(content=content, reasoning_content=reasoning_content)


def test_synth_reads_reasoning_when_content_empty():
    from core.script_synthesize import _strip_fences
    from core.precondition import extract_response_text

    body = "```python\nasync def test_case_5(page):\n    pass\n```"
    assert "async def" in _strip_fences(extract_response_text(_msg("", body)))


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


def test_accept_rejects_url_with_cjk_bracket():
    """描述里的「】」不得进入 URL 字面值（曾实测：goto 带上 】→ 404 才回落 OTA）。"""
    from core.script_synthesize import _accept_synthesized_script

    bad = (
        "async def test_case_9(page):\n"
        '    await page.goto("http://localhost:8030/upload.html】")\n'
    )
    with pytest.raises(ValueError) as ei:
        _accept_synthesized_script(bad, case_id=9)
    assert "CJK bracket" in str(ei.value)

    good = (
        "async def test_case_9(page):\n"
        '    await page.goto("http://localhost:8030/upload.html")\n'
    )
    assert "async def" in _accept_synthesized_script(good, case_id=9)


def test_required_targets_ignore_fullwidth_paren_fragment():
    """036 回归：括号内「点击导出会触发文件下载）」曾被误提取为必现目标字符串
    （导致合法合成脚本被覆盖检查拒绝、回落 OTA）。"""
    from core.script_synthesize import extract_required_targets

    steps = [
        {"step_order": 1, "description": "打开【报表页】"},
        {"step_order": 2, "description": "导出报表为 CSV 文件（点击导出会触发文件下载）"},
        {"step_order": 3, "description": "断言页面显示【报表页】"},
    ]
    targets = extract_required_targets(steps)
    assert "报表页" in targets
    assert not any("会触发文件下载" in t for t in targets)

    # 常规句式不受影响
    normal = extract_required_targets(
        [{"description": "点击登录"}, {"description": "选择京州市院"}]
    )
    assert "登录" in normal and "京州市院" in normal
