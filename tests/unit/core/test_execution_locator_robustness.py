"""执行成功率修复的回归测试。

覆盖三处真实线上失败（证据来自 reports/**/report.json 的错误语料）：
1. ``waiting for locator("f3e24")`` — iframe 内快照 ref 漏判，被当成持久定位符
   写进固化脚本。根因：``(?:e|f5e|ref_|probe_idx_)\\d+`` 只认 frame 5。
2. ``Unknown action: error`` — 客户端未处理 LLM 的 error 控制信号，
   丢掉失败原因（服务端 core/playwright_manager.py 是处理的）。
3. ``Frame.wait_for_selector() missing 1 required positional argument`` —
   合成脚本未被校验就入库，下次直跑必然失败。
"""

from __future__ import annotations

import pytest

from core.codegen_locator import normalize_playwright_locator
from core.locator_candidates import is_ephemeral_locator_ref, is_snapshot_ref
from core.replay_resolve import is_ephemeral_ref
from core.script_synthesize import (
    _accept_synthesized_script,
    check_script_sanity,
    check_script_warnings,
)

# ── 1. 快照 ref 判定 ─────────────────────────────────────────────────────────

# 真实 ref 形态：e12（主 frame）/ f<帧号>e<元素号>（iframe 内）
SNAPSHOT_REFS = ["e12", "e1", "f1e3", "f3e24", "f5e10", "f12e345"]
EPHEMERAL_NON_SNAPSHOT = ["ref_3", "probe_idx_1", "PROBE_IDX_2"]
# Playwright 侧的等价写法（MCP 内部即 page.locator('aria-ref=e3')），同样只当次有效
ARIA_REF_SELECTORS = [
    "aria-ref=e12",
    "aria-ref=f3e24",
    "aria-ref=e1 >> internal:control=enter-frame",
]
DURABLE_LOCATORS = [
    "get_by_role(\"button\", name=\"登录\")",
    "page.get_by_placeholder(\"请输入\")",
    "[data-testid=submit]",
    "#login-btn",
    "text=提交",
    "slide12",
]


@pytest.mark.parametrize(
    "ref", SNAPSHOT_REFS + EPHEMERAL_NON_SNAPSHOT + ARIA_REF_SELECTORS
)
def test_ephemeral_refs_detected(ref):
    assert is_ephemeral_locator_ref(ref) is True


@pytest.mark.parametrize("loc", DURABLE_LOCATORS)
def test_durable_locators_not_flagged(loc):
    assert is_ephemeral_locator_ref(loc) is False


@pytest.mark.parametrize("ref", SNAPSHOT_REFS)
def test_every_frame_ref_is_a_snapshot_ref(ref):
    """回归护栏：旧正则只认 f5e*，其他帧号的 ref 全部漏判。"""
    assert is_snapshot_ref(ref) is True


def test_normalize_rejects_iframe_ref_locator():
    """活代码泄漏点：iframe ref 曾通过校验 → page.locator("f3e24") → 找不到元素。"""
    assert normalize_playwright_locator("f3e24") is None
    assert normalize_playwright_locator("f1e3") is None
    assert normalize_playwright_locator("page.f3e24") is None
    assert normalize_playwright_locator("e12") is None
    assert normalize_playwright_locator("ref_3") is None
    assert normalize_playwright_locator("probe_idx_1") is None
    # 真实定位符必须保留
    assert normalize_playwright_locator("get_by_role('button', name='登录')") == (
        "get_by_role('button', name='登录')"
    )
    assert normalize_playwright_locator("page.#login") == "#login"


def test_replay_resolve_uses_canonical_predicate():
    for ref in SNAPSHOT_REFS + EPHEMERAL_NON_SNAPSHOT + ARIA_REF_SELECTORS:
        assert is_ephemeral_ref(ref) is True
    for loc in DURABLE_LOCATORS:
        assert is_ephemeral_ref(loc) is False


def test_aria_ref_selector_not_persisted():
    """aria-ref=e12 是 MCP 内部真实使用的选择器语法，同样不能被固化。"""
    assert normalize_playwright_locator("aria-ref=e12") is None
    assert normalize_playwright_locator("aria-ref=f3e24") is None


# ── 2. 合成脚本静态体检 ──────────────────────────────────────────────────────

BAD_SCRIPT_CASES = {
    "bare_ref_locator": (
        "async def test_case_1(page) -> None:\n"
        "    await page.locator(\"e3\").click()\n",
        "ephemeral snapshot ref",
    ),
    "aria_ref_locator": (
        "async def test_case_1(page) -> None:\n"
        "    await page.locator(\"aria-ref=e12\").click()\n",
        "ephemeral snapshot ref",
    ),
    "iframe_ref_wait": (
        "async def test_case_1(page) -> None:\n"
        "    await page.wait_for_selector(\"f3e24\")\n",
        "ephemeral snapshot ref",
    ),
    "empty_selector_call": (
        "async def test_case_1(page) -> None:\n"
        "    await page.wait_for_selector()\n",
        "without a selector",
    ),
    "undefined_frame": (
        "async def test_case_1(page) -> None:\n"
        "    await frame.locator(\"#a\").click()\n",
        "undefined variable 'frame'",
    ),
    "syntax_error": (
        "async def test_case_1(page) -> None\n"
        "    pass\n",
        "not valid Python",
    ),
}

GOOD_SCRIPT = (
    "from playwright.async_api import expect\n\n"
    "async def test_case_1(page) -> None:\n"
    "    page.set_default_timeout(30000)\n"
    "    await page.goto(\"https://example.com\")\n"
    "    await page.get_by_role(\"button\", name=\"登录\").first.click()\n"
    "    await page.get_by_placeholder(\"请输入用户名\").first.fill(\"admin\")\n"
)


@pytest.mark.parametrize(
    "script,fragment", BAD_SCRIPT_CASES.values(), ids=list(BAD_SCRIPT_CASES)
)
def test_sanity_flags_unrunnable_scripts(script, fragment):
    problems = check_script_sanity(script)
    assert any(fragment in p for p in problems), problems


def test_sanity_accepts_realistic_script():
    assert check_script_sanity(GOOD_SCRIPT) == []


def test_sanity_allows_legitimate_frame_loop():
    """for frame in page.frames 是合法用法，不能误判为未定义变量。"""
    script = (
        "async def test_case_1(page) -> None:\n"
        "    for frame in page.frames:\n"
        "        await frame.locator(\"#a\").click()\n"
    )
    assert check_script_sanity(script) == []


def test_sanity_rejects_unawaited_playwright_assertion():
    """037：漏 await 的 expect(...).to_be_visible() 不执行断言 → 假通过，必须拒收。"""
    script = (
        "async def test_case_1(page) -> None:\n"
        "    expect(page.get_by_text(\"登录成功\").first).to_be_visible()\n"
    )
    problems = check_script_sanity(script)
    assert any("un-awaited Playwright assertion" in p for p in problems), problems


def test_sanity_accepts_awaited_assertion():
    script = (
        "from playwright.async_api import expect\n"
        "async def test_case_1(page) -> None:\n"
        "    await expect(page.get_by_text(\"登录成功\").first).to_be_visible()\n"
    )
    assert check_script_sanity(script) == []


def test_fixed_waits_are_warnings_not_rejections():
    script = (
        "async def test_case_1(page) -> None:\n"
        "    await page.wait_for_timeout(3000)\n"
    )
    assert check_script_sanity(script) == []
    assert check_script_warnings(script) != []


def test_accept_rejects_bad_script_and_passes_good_one():
    with pytest.raises(ValueError) as exc:
        _accept_synthesized_script(BAD_SCRIPT_CASES["bare_ref_locator"][0], case_id=7)
    assert "sanity check" in str(exc.value)

    assert _accept_synthesized_script(GOOD_SCRIPT, case_id=7) == GOOD_SCRIPT


# ── 3. 客户端 error/done 控制信号 ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_client_preserves_llm_error_reason():
    """服务端返回 'LLM error: <原因>'；客户端曾返回 'Unknown action: error'。"""
    from agent.client_core import AgentClient

    client = object.__new__(AgentClient)

    async def _unused_tool_call(*args, **kwargs):  # pragma: no cover
        raise AssertionError("error action must not reach MCP")

    client._mcp_tools_call = _unused_tool_call

    result = await client._mcp_call_tool(
        action="error", selector="", value="页面被安全验证拦截，无法继续",
    )

    assert result["success"] is False
    assert result["error"].startswith("LLM error:")
    assert "页面被安全验证拦截" in result["error"]


@pytest.mark.asyncio
async def test_client_reports_done_signal_clearly():
    from agent.client_core import AgentClient

    client = object.__new__(AgentClient)

    async def _unused_tool_call(*args, **kwargs):  # pragma: no cover
        raise AssertionError("done action must not reach MCP")

    client._mcp_tools_call = _unused_tool_call

    result = await client._mcp_call_tool(action="done", selector="", value="")

    assert result["success"] is False
    assert "done" in result["error"]
    assert "Unknown action" not in result["error"]


@pytest.mark.asyncio
async def test_client_dispatch_reaches_mcp_without_name_error():
    """回归（036 映射发现）：_mcp_call_tool 曾引用未定义的 role/field_type →
    任何非 error/done 动作派发都 NameError（B1 引入，旧测试只覆盖早返回分支）。"""
    from agent.client_core import AgentClient

    client = object.__new__(AgentClient)
    calls = []

    async def _record_tool_call(tool_name, arguments):
        calls.append((tool_name, arguments))
        return {"success": True, "text": "### Result\n{}"}

    client._mcp_tools_call = _record_tool_call

    result = await client._mcp_call_tool(action="snapshot", selector="", value="")

    assert result["success"] is True
    assert calls and calls[0][0] == "browser_snapshot"


@pytest.mark.asyncio
async def test_client_threads_role_field_type_into_mcp_args():
    """verify_visible 的 role、verify_value 的 field_type 必须贯通到 MCP 参数。"""
    from agent.client_core import AgentClient

    client = object.__new__(AgentClient)
    calls = []

    async def _record_tool_call(tool_name, arguments):
        calls.append((tool_name, arguments))
        return {"success": True, "text": "### Result\n{}"}

    client._mcp_tools_call = _record_tool_call

    await client._mcp_call_tool(
        action="verify_visible", selector="", value="登录", role="button",
    )
    assert calls[0][0] == "browser_verify_element_visible"
    assert calls[0][1].get("role") == "button"

    await client._mcp_call_tool(
        action="verify_value", selector="e5", value="admin", field_type="textbox",
    )
    assert calls[1][0] == "browser_verify_value"
    assert calls[1][1].get("type") == "textbox"
