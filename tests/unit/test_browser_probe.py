"""DOM 探针（移植自 ZCode snapshot 纪律）：富元素采集 → 确定性回退定位符。

参考 zai-org/ZCode（Apache-2.0）adapters/src/browser/snapshot.ts 的元素采集与
refs 纪律；本实现走我们的 browser_evaluate 通道（无 ElementHandle，故不产 ref）。
"""
from __future__ import annotations

import json

import pytest


# ── 纯函数 ───────────────────────────────────────────────────────────────


def test_derive_fallbacks_order_and_dedup():
    from core.browser_probe import derive_fallbacks

    entry = {
        "name": "Add to cart",
        "selector": "button:nth-of-type(1) > span:nth-of-type(2)",
        "xpath": "/html/body/div[2]/button[1]",
        "attributes": {"data-testid": "add-to-cart", "id": "addBtn"},
    }
    fb = derive_fallbacks(entry)
    assert fb[0] == '[data-testid="add-to-cart"]'
    assert "#addBtn" in fb
    assert "text=Add to cart" in fb
    assert fb.count("text=Add to cart") == 1


def test_derive_fallbacks_skips_unsafe_id():
    from core.browser_probe import derive_fallbacks

    fb = derive_fallbacks({"name": "x", "attributes": {"id": "1bad id"}, "selector": "", "xpath": ""})
    assert not any(x.startswith("#") for x in fb)


def test_match_target_exact_then_contains():
    from core.browser_probe import match_target

    elements = [
        {"name": "Add to cart (#1)", "text": "Add to cart (#1)"},
        {"name": "Add to cart", "text": "Add to cart"},
        {"name": "Remove", "text": "Remove"},
    ]
    got = match_target(elements, "Add to cart")
    assert got and got[0]["name"] == "Add to cart", "精确匹配必须排第一"


def _caller_returning(payload: dict):
    async def _caller(tool: str, args: dict):
        assert tool == "browser_evaluate"
        return {"success": True, "text": "### Result\n" + json.dumps(payload)}

    return _caller


# ── 探针（异步） ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_probe_fallbacks_single_match():
    from core.browser_probe import probe_fallbacks

    payload = {"url": "https://x/", "title": "t", "count": 2, "elements": [
        {"name": "Login", "selector": "#login-btn", "xpath": "/html/input[2]",
         "attributes": {"id": "login-btn", "data-testid": "login"}},
        {"name": "Other", "selector": "#other", "xpath": "/html/input[3]", "attributes": {}},
    ]}
    locs, ev = await probe_fallbacks(_caller_returning(payload), "Login")
    assert ev["matched"] == 1
    assert locs[0] == '[data-testid="login"]' and "#login-btn" in locs


@pytest.mark.asyncio
async def test_probe_fallbacks_zero_match_reports_evidence():
    from core.browser_probe import probe_fallbacks

    payload = {"url": "https://x/", "title": "t", "count": 3, "elements": [
        {"name": "A", "selector": "#a", "xpath": "/a", "attributes": {}},
    ]}
    locs, ev = await probe_fallbacks(_caller_returning(payload), "不存在的按钮")
    assert locs == [] and ev["matched"] == 0 and ev["elements"] == 3


@pytest.mark.asyncio
async def test_probe_fallbacks_ambiguous_no_guess():
    from core.browser_probe import probe_fallbacks

    payload = {"url": "https://x/", "title": "t", "count": 2, "elements": [
        {"name": "Add to cart (1)", "selector": "#a1", "xpath": "/a1", "attributes": {}},
        {"name": "Add to cart (2)", "selector": "#a2", "xpath": "/a2", "attributes": {}},
    ]}
    locs, ev = await probe_fallbacks(_caller_returning(payload), "Add to cart")
    assert locs == [] and ev.get("ambiguous") is True


# ── 单元执行里的探针回退 ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_unit_probe_fallback_path():
    """主定位失败 → 探针派生定位符 → 再试一轮成功（且带证据入账）。"""
    from core.action_unit import UnitStep, run_unit

    step = UnitStep(
        index=0, action="click", selector="e99", value=None,
        element_desc="Add to cart", fallbacks=[],
        raw={"action": "click", "selector": "e99", "element_desc": "Add to cart"},
    )
    calls: list[str] = []

    async def _exec(action: dict):
        sel = str(action.get("selector"))
        calls.append(sel)
        if sel == "#add-to-cart":
            return {"success": True}
        return {"success": False, "error": "strict mode violation: resolved to 0 elements"}

    async def _probe(target: str):
        assert target == "Add to cart"
        return ['#add-to-cart'], {"elements": 5, "matched": 1}

    outcome = await run_unit([step], _exec, probe=_probe, retry_delays=())
    assert outcome.success
    assert calls[0] == "e99" and calls[-1] == "#add-to-cart"
    assert outcome.executed[0].get("_probe", {}).get("matched") == 1


@pytest.mark.asyncio
async def test_run_unit_probe_zero_match_fails_with_evidence():
    from core.action_unit import UnitStep, run_unit

    step = UnitStep(index=0, action="click", selector="e99", value=None,
                    element_desc="幽灵按钮", fallbacks=[],
                    raw={"action": "click", "selector": "e99"})

    async def _exec(action: dict):
        return {"success": False, "error": "strict mode violation: resolved to 0 elements"}

    async def _probe(target: str):
        return [], {"elements": 7, "matched": 0}

    outcome = await run_unit([step], _exec, probe=_probe, retry_delays=())
    assert not outcome.success
    assert "DOM 探针" in (outcome.error or "") and "匹配 0" in (outcome.error or "")
