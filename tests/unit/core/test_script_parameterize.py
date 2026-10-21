"""039：参数化固化脚本（占位符包装/入口 params）。"""
from __future__ import annotations

import asyncio

from core.script_parameterize import parameterize_script, script_uses_parameters


SCRIPT = '''async def test_case_7(page) -> None:
    """case"""
    await page.fill("#u", "{{username}}")
    await page.fill("#p", "{{ password }}")
    await page.click("button")
'''


def test_detects_parameters():
    assert script_uses_parameters(SCRIPT)
    assert not script_uses_parameters('await page.click("x")')


def test_entry_accepts_params_and_literals_wrapped():
    out = parameterize_script(SCRIPT, case_id=7)
    assert "async def test_case_7(page, params=None):" in out
    assert "_vt(\"{{username}}\")" in out
    assert "_vt(\"{{ password }}\")" in out
    assert "global _VT_PARAMS" in out


def test_runtime_substitution():
    out = parameterize_script(SCRIPT, case_id=7)
    ns: dict = {}
    exec(compile(out, "<test>", "exec"), ns, ns)

    class Page:
        def __init__(self):
            self.values = {}

        async def fill(self, selector, value):
            self.values[selector] = value

        async def click(self, selector):
            pass

    page = Page()
    asyncio.run(ns["test_case_7"](page, {"username": "alice", "password": "pw"}))
    assert page.values == {"#u": "alice", "#p": "pw"}

    page2 = Page()
    asyncio.run(ns["test_case_7"](page2))
    assert page2.values == {"#u": "{{username}}", "#p": "{{ password }}"}


def test_mixed_literal_is_wrapped_and_substituted():
    script = 'async def test_case_8(page):\n    await page.get_by_text("欢迎 {{username}}")\n'
    out = parameterize_script(script, case_id=8)
    assert '_vt("欢迎 {{username}}")' in out
    ns: dict = {}
    exec(compile(out, "<test>", "exec"), ns, ns)
    assert ns["_vt"]("欢迎 {{username}}") == "欢迎 {{username}}"
    ns["_VT_PARAMS"] = {"username": "admin"}
    assert ns["_vt"]("欢迎 {{username}}") == "欢迎 admin"


def test_idempotent():
    once = parameterize_script(SCRIPT, case_id=7)
    twice = parameterize_script(once, case_id=7)
    assert twice.count("_VT_PARAMS") >= 2
    assert twice.count("async def test_case_7(page, params=None):") == 1
