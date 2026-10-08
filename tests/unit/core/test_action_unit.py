"""Phase B：宏动作单元（规范化 / 备选定位 / 本地重试 / 止损语义）。"""
from __future__ import annotations

import pytest

from core.action_unit import (
    UnitStep,
    expand_fallbacks,
    normalize_unit,
    run_unit,
)

RAW = [
    {"action": "fill", "selector": "e11", "value": "standard_user",
     "fallbacks": ["text=Username", "input#user-name"]},
    {"action": "click", "selector": "e15"},
    {"action": "assert_text", "selector": "e40", "value": "Products"},
]


def test_normalize_keeps_valid_and_reports_drops():
    steps, problems = normalize_unit(
        RAW + [
            {"action": "evaluate", "value": "x"},          # 动作不允许
            {"action": "fill", "value": "y"},              # 缺 selector
            {"action": "click", "selector": "e99"},        # ref 不在候选
            {"action": "assert_text", "selector": "e40"},  # 缺 value
        ],
        max_steps=6,
        is_valid_ref=lambda ref: ref != "e99",
    )
    assert [s.action for s in steps] == ["fill", "click", "assert_text"]
    assert steps[0].fallbacks == ["text=Username", "input#user-name"]
    assert len(problems) == 4


def test_normalize_truncates_to_max():
    many = [{"action": "click", "selector": f"e{i}"} for i in range(1, 11)]
    steps, problems = normalize_unit(many, max_steps=3, is_valid_ref=lambda _r: True)
    assert len(steps) == 3
    assert any("上限" in p for p in problems)


def test_expand_fallbacks_order():
    steps, _ = normalize_unit(RAW[:1], max_steps=6, is_valid_ref=lambda _r: True)
    attempts = expand_fallbacks(steps[0])
    assert [a["selector"] for a in attempts] == ["e11", "text=Username", "input#user-name"]


@pytest.mark.asyncio
async def test_run_unit_all_success():
    steps, _ = normalize_unit(RAW, max_steps=6, is_valid_ref=lambda _r: True)
    calls, recorded = [], []

    async def _exec(action):
        calls.append(action["action"])
        return {"success": True}

    async def _ok(action, _result):
        recorded.append(action["action"])

    out = await run_unit(steps, _exec, on_step_ok=_ok)
    assert out.success and out.executed_count == 3
    assert calls == ["fill", "click", "assert_text"]
    assert recorded == ["fill", "click", "assert_text"]


@pytest.mark.asyncio
async def test_run_unit_stops_at_first_failure():
    steps, _ = normalize_unit(RAW, max_steps=6, is_valid_ref=lambda _r: True)
    seen = []

    async def _exec(action):
        seen.append(action["action"])
        if action["action"] == "click":
            return {"success": False, "error": "strict mode violation: resolved to 2 elements"}
        return {"success": True}

    out = await run_unit(steps, _exec)
    assert not out.success
    assert out.failed_index == 1 and out.executed_count == 1
    assert "strict mode" in (out.error or "")
    assert "assert_text" not in seen          # 第 3 步不再尝试


@pytest.mark.asyncio
async def test_run_unit_transient_retry_before_giving_up():
    steps, _ = normalize_unit(RAW[1:2], max_steps=6, is_valid_ref=lambda _r: True)
    attempts = []
    slept = []

    async def _exec(_action):
        attempts.append(1)
        if len(attempts) == 1:
            return {"success": False, "error": "Timeout 30000ms exceeded"}
        return {"success": True}

    async def _sleep(sec):
        slept.append(sec)

    out = await run_unit(steps, _exec, sleep=_sleep)
    assert out.success
    assert len(attempts) == 2            # 瞬时失败本地重试后成功
    assert slept == [0.6]


@pytest.mark.asyncio
async def test_run_unit_uses_fallback_locator():
    raw = [{"action": "click", "selector": "e15", "fallbacks": ["text=Login"]}]
    steps, _ = normalize_unit(raw, max_steps=6, is_valid_ref=lambda _r: True)
    seen = []

    async def _exec(action):
        seen.append(action["selector"])
        if action["selector"] == "e15":
            return {"success": False, "error": "strict mode violation"}
        return {"success": True}

    out = await run_unit(steps, _exec)
    assert out.success and out.executed_count == 1
    assert seen == ["e15", "text=Login"]
    assert out.executed[0]["selector"] == "text=Login"   # 生效的是备选定位符


# ── Phase B(auto)：确定性自动成组 ──────────────────────────────────────────


class _Cand:
    def __init__(self, ref, role, name, visible=True, enabled=True):
        self.ref, self.role, self.name = ref, role, name
        self.text = name
        self.visible, self.enabled = visible, enabled


CANDIDATES = [
    _Cand("e11", "textbox", "Username"),
    _Cand("e13", "textbox", "Password"),
    _Cand("e15", "button", "Login"),
    _Cand("e38", "combobox", "Product Sort By"),
    _Cand("e53", "link", "Cart"),
    _Cand("e54", "button", "Add to cart"),
]

STEPS_516 = [
    {"step_order": 1, "description": "打开登录页面"},
    {"step_order": 2, "description": "在【Username】输入框输入「standard_user」"},
    {"step_order": 3, "description": "在【Password】密码框输入「secret_sauce」"},
    {"step_order": 4, "description": "点击【Login】登录按钮"},
    {"step_order": 5, "description": "断言页面显示用户名密码不匹配提示"},
]


def test_parse_step_intent_basics():
    from core.script_precompile import parse_step_intent as p

    assert p("在【Username】输入框输入「standard_user」") == {
        "kind": "fill", "target": "Username", "value": "standard_user"
    }
    assert p("点击【Login】登录按钮")["kind"] == "click"
    assert p("在【Product Sort By】中选择「Name (Z to A)」")["kind"] == "select"
    assert p("按键 ESC") == {"kind": "press_key", "key": "Escape"}
    assert p("断言页面包含【Products】") == {"kind": "assert_contains", "text": "Products"}
    assert p("打开登录页面")["kind"] == "open"          # 入口页可机械解析
    assert p("导航至商品列表页面") is None               # 需要已登录路径 → 交给 LLM
    assert p("断言购物车图标徽标数字为【1】") is None


def test_build_auto_unit_full_mechanical_run():
    from core.action_unit import build_auto_unit

    actions, notes = build_auto_unit(
        STEPS_516, set(), CANDIDATES,
        base_url="https://www.saucedemo.com",
    )
    assert actions is not None
    kinds = [(a["action"], a.get("selector"), a.get("value")) for a in actions]
    assert kinds[0] == ("goto", None, "https://www.saucedemo.com")
    assert kinds[1] == ("fill", "e11", "standard_user")
    assert kinds[2] == ("fill", "e13", "secret_sauce")
    assert kinds[3] == ("click", "e15", None)
    assert len(actions) == 4                      # 断言行不可机械 → 在它之前停止
    assert any("无法机械解析" in n for n in notes)


def test_build_auto_unit_skips_covered_and_requires_min():
    from core.action_unit import build_auto_unit

    # 覆盖 1-3 步 → 只剩 click（1 步）< min_steps=2 → 不成组
    actions, _ = build_auto_unit(
        STEPS_516, {1, 2, 3}, CANDIDATES,
        base_url="https://www.saucedemo.com",
    )
    assert actions is None
    # 覆盖 1-2 步 → fill + click = 2 步 → 成组
    actions2, _ = build_auto_unit(
        STEPS_516, {1, 2}, CANDIDATES,
        base_url="https://www.saucedemo.com",
    )
    assert actions2 and [a["action"] for a in actions2] == ["fill", "click"]


def test_build_auto_unit_stops_on_missing_or_ambiguous_target():
    from core.action_unit import build_auto_unit

    # 目标不在候选里 → 立即停止
    steps = [
        {"step_order": 1, "description": "在【Username】输入框输入「u」"},
        {"step_order": 2, "description": "在【不存在字段】输入框输入「x」"},
        {"step_order": 3, "description": "点击【Login】登录按钮"},
    ]
    actions, notes = build_auto_unit(steps, set(), CANDIDATES, base_url="https://e.com")
    assert actions is None                       # 只有 1 步 → 不成组
    assert any("找不到目标" in n for n in notes)

    # 同名两个候选 → 歧义 → 放弃该步（安全第一）
    dup = [
        _Cand("e11", "textbox", "Username"),
        _Cand("e12", "textbox", "Username"),
        _Cand("e15", "button", "Login"),
    ]
    steps2 = [
        {"step_order": 1, "description": "在【Username】输入框输入「u」"},
        {"step_order": 2, "description": "点击【Login】登录按钮"},
    ]
    actions2, notes2 = build_auto_unit(steps2, set(), dup, base_url="https://e.com")
    assert actions2 is None
    assert any("找不到目标「Username」" in n for n in notes2)


# ── DOM 探针回退（静态 fallbacks 用尽后的最后确定性尝试）────────────────────


@pytest.mark.asyncio
async def test_run_unit_probe_fallback_recovers():
    raw = [{"action": "click", "selector": "e15", "element_desc": "Login"}]
    steps, _ = normalize_unit(raw, max_steps=6, is_valid_ref=lambda _r: True)
    seen = []

    async def _exec(action):
        seen.append(action["selector"])
        return {"success": action["selector"] == "#login-btn"}

    async def _probe(target):
        assert target == "Login"
        return ["text=Login", "#login-btn"], {"elements": 7, "matched": 1}

    out = await run_unit(steps, _exec, probe=_probe)
    assert out.success and out.executed_count == 1
    assert seen == ["e15", "text=Login", "#login-btn"]
    assert out.executed[0]["selector"] == "#login-btn"
    assert out.executed[0]["_probe"] == {"elements": 7, "matched": 1}


@pytest.mark.asyncio
async def test_run_unit_probe_evidence_reported_when_not_found():
    raw = [{"action": "click", "selector": "e15", "element_desc": "Login"}]
    steps, _ = normalize_unit(raw, max_steps=6, is_valid_ref=lambda _r: True)

    async def _exec(_action):
        return {"success": False, "error": "strict mode violation"}

    async def _probe(_target):
        return [], {"elements": 3, "matched": 0}

    out = await run_unit(steps, _exec, probe=_probe)
    assert not out.success
    assert "strict mode violation" in out.error
    assert "DOM 探针" in out.error and "匹配 0" in out.error


@pytest.mark.asyncio
async def test_run_unit_probe_exception_does_not_crash():
    raw = [{"action": "click", "selector": "e15", "element_desc": "Login"}]
    steps, _ = normalize_unit(raw, max_steps=6, is_valid_ref=lambda _r: True)

    async def _exec(_action):
        return {"success": False, "error": "boom"}

    async def _probe(_target):
        raise RuntimeError("probe exploded")

    out = await run_unit(steps, _exec, probe=_probe)
    assert not out.success and out.error == "boom"
