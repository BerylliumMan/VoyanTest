"""039：参数化脚本 helper。"""
from __future__ import annotations

from core.script_parameterize import parameterize_script


def test_exact_literal_wrapped():
    script = 'async def test_case_1(page):\n    await page.fill("#u", "{{username}}")\n'
    out = parameterize_script(script, case_id=1)
    assert "_vt(\"{{username}}\")" in out


def test_no_placeholder_untouched():
    script = 'async def test_case_1(page):\n    await page.click("登录")\n'
    assert 'page.click("登录")' in parameterize_script(script, case_id=1)
