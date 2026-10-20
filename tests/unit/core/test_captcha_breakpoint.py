"""037 T020：验证码断点混合执行（captcha breakpoint）单测。

覆盖：
- 纯函数：captcha_breakpoint_index / case_requires_captcha / captcha_tail_steps
- meta 持久化：persist_compiled_script(meta=...) / clear_compiled_script 清 meta
- bridge：_load_breakpoint_state 装载条件（hash/开关/run_script 排除）
- bridge：_journal_head_covered / _on_breakpoint_head_ready（成功=完成；失败=清断点续 OTA）
- bridge：断点武装时 _try_replay_compiled_script 跳过常规回放
- manager：skip_initial_goto 进 payload
"""
from __future__ import annotations

import types

import pytest

from app import db_models  # noqa: F401 —— 注册全部表

BASE = "http://captcha.local"

CAPTCHA_STEPS = [
    {"step_order": 1, "description": "打开【登录页】页面 http://captcha.local/login", "parsed_result": ""},
    {"step_order": 2, "description": "用账号 admin、密码 x 登录（含图形验证码，按顺序填写）", "parsed_result": "登录成功"},
    {"step_order": 3, "description": "断言页面出现【登录成功】", "parsed_result": "显示欢迎"},
    {"step_order": 4, "description": "点击【我的资料】菜单", "parsed_result": "打开资料页"},
]

TAIL_SCRIPT = '''from playwright.async_api import expect

async def test_case_900(page) -> None:
    page.set_default_timeout(30000)
    page.set_default_navigation_timeout(60000)
    await expect(page.get_by_text("登录成功").first).to_be_visible(timeout=15000)
    await page.get_by_role("link", name="我的资料").first.click()
'''


# ── A. 纯函数 ────────────────────────────────────────────────────────────


def test_captcha_breakpoint_index_and_tail():
    from core.script_synthesize import (
        captcha_breakpoint_index,
        captcha_tail_steps,
        case_requires_captcha,
    )

    assert captcha_breakpoint_index(CAPTCHA_STEPS) == 2
    assert case_requires_captcha(CAPTCHA_STEPS) is True
    tail = captcha_tail_steps(CAPTCHA_STEPS)
    assert [s["step_order"] for s in tail] == [3, 4]
    assert captcha_breakpoint_index([{"step_order": 1, "description": "打开首页"}]) is None
    assert case_requires_captcha([]) is False
    assert captcha_tail_steps([{"step_order": 1, "description": "打开首页"}]) == []


def test_captcha_tail_empty_when_last_step_is_captcha():
    from core.script_synthesize import captcha_tail_steps

    steps = [
        {"step_order": 1, "description": "打开登录页"},
        {"step_order": 2, "description": "输入验证码并登录"},
    ]
    assert captcha_tail_steps(steps) == []


# ── B. meta 持久化 ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_persist_and_clear_meta(db):
    from core.compiled_script import clear_compiled_script, persist_compiled_script

    proj = db_models.Project(name="bp", base_url=BASE)
    db.add(proj)
    await db.commit()
    await db.refresh(proj)
    case = db_models.TestCase(project_id=proj.id, name="断点用例", priority="high", case_kind="ui")
    db.add(case)
    await db.commit()
    await db.refresh(case)

    persist_compiled_script(case, script=TAIL_SCRIPT, steps_hash="h1", meta={"captcha_segment_start": 2})
    assert case.compiled_script_meta == {"captcha_segment_start": 2}

    changed = clear_compiled_script(case)
    assert changed is True
    assert case.compiled_script is None and case.compiled_script_meta is None


@pytest.mark.asyncio
async def test_persist_without_meta_sets_none(db):
    from core.compiled_script import persist_compiled_script

    proj = db_models.Project(name="bp2", base_url=BASE)
    db.add(proj)
    await db.commit()
    await db.refresh(proj)
    case = db_models.TestCase(project_id=proj.id, name="普通用例", priority="high", case_kind="ui")
    db.add(case)
    await db.commit()
    await db.refresh(case)
    persist_compiled_script(case, script="s", steps_hash="h")
    assert case.compiled_script_meta is None


# ── C. bridge 装载条件 ───────────────────────────────────────────────────


async def _make_bridge_with_case(db, *, meta, script=TAIL_SCRIPT, hash_ok=True, case_kind="ui"):
    from core.agent_bridge import AgentBridge
    from core.compiled_script import steps_content_hash

    proj = db_models.Project(name="bpc", base_url=BASE)
    db.add(proj)
    await db.commit()
    await db.refresh(proj)
    case = db_models.TestCase(project_id=proj.id, name="断点装载", priority="high", case_kind=case_kind)
    db.add(case)
    await db.commit()
    await db.refresh(case)
    for i, s in enumerate(CAPTCHA_STEPS, start=1):
        db.add(db_models.TestStep(case_id=case.id, step_order=i,
                                  description=s["description"],
                                  parsed_result=s["parsed_result"] or None))
    await db.commit()
    await db.refresh(case)

    step_dicts = [
        {
            "id": None,
            "step_order": s["step_order"],
            "description": s["description"],
            # 与 bridge._steps_to_dicts 逐字一致：DB 行 parsed_result=None 时 expected_result=None
            "expected_result": s["parsed_result"] or None,
            "learned_locator": None,
            "structured_step": None,
            "cacheable": True,
        }
        for s in CAPTCHA_STEPS
    ]
    h = steps_content_hash(step_dicts) if hash_ok else "wrong-hash"
    from core.compiled_script import persist_compiled_script

    if script is not None:
        persist_compiled_script(case, script=script, steps_hash=h, meta=meta)
    await db.commit()

    bridge = AgentBridge.__new__(AgentBridge)
    bridge.db = db
    bridge.agent_manager = types.SimpleNamespace()
    bridge._breakpoint_until = None
    bridge._breakpoint_tail_script = None
    bridge._breakpoint_tail_steps = []
    bridge._breakpoint_replay_logs = []
    return bridge, case


def _patch_settings(monkeypatch, **overrides):
    from app.config import Settings

    settings = Settings(**overrides)
    monkeypatch.setattr("app.config.get_settings", lambda: settings)


@pytest.mark.asyncio
async def test_load_breakpoint_state_armed(db, monkeypatch):
    _patch_settings(monkeypatch, exec_captcha_breakpoint=True)
    bridge, case = await _make_bridge_with_case(db, meta={"captcha_segment_start": 2})
    await bridge._load_breakpoint_state(case.id)
    assert bridge._breakpoint_until == 2
    assert bridge._breakpoint_tail_script == TAIL_SCRIPT.strip() or bridge._breakpoint_tail_script
    assert [s["step_order"] for s in bridge._breakpoint_tail_steps] == [3, 4]


@pytest.mark.asyncio
async def test_load_breakpoint_state_disabled(db, monkeypatch):
    _patch_settings(monkeypatch, exec_captcha_breakpoint=False)
    bridge, case = await _make_bridge_with_case(db, meta={"captcha_segment_start": 2})
    await bridge._load_breakpoint_state(case.id)
    assert bridge._breakpoint_until is None


@pytest.mark.asyncio
async def test_load_breakpoint_state_hash_mismatch(db, monkeypatch):
    _patch_settings(monkeypatch, exec_captcha_breakpoint=True)
    bridge, case = await _make_bridge_with_case(db, meta={"captcha_segment_start": 2}, hash_ok=False)
    await bridge._load_breakpoint_state(case.id)
    assert bridge._breakpoint_until is None


@pytest.mark.asyncio
async def test_load_breakpoint_state_no_meta(db, monkeypatch):
    _patch_settings(monkeypatch, exec_captcha_breakpoint=True)
    bridge, case = await _make_bridge_with_case(db, meta=None)
    await bridge._load_breakpoint_state(case.id)
    assert bridge._breakpoint_until is None


# ── D. 头段覆盖判定与断点完成/回退 ──────────────────────────────────────


@pytest.mark.asyncio
async def test_head_covered_and_tail_success_completes(db, monkeypatch):
    _patch_settings(monkeypatch, exec_captcha_breakpoint=True)
    bridge, case = await _make_bridge_with_case(db, meta={"captcha_segment_start": 2})
    await bridge._load_breakpoint_state(case.id)
    bridge._run_steps = [
        {"step_order": s["step_order"], "description": s["description"]} for s in CAPTCHA_STEPS
    ]
    bridge._journal_covered = {1, 2}
    assert bridge._journal_head_covered() is True

    calls = []

    class _FakeAM:
        async def _try_run_compiled_script(self, agent_id, run_id_str, name, steps, **kw):
            calls.append(kw)
            return [
                {"success": True, "original_description": "断言页面出现【登录成功】"},
                {"success": True, "original_description": "点击【我的资料】菜单"},
            ]

    bridge.agent_manager = _FakeAM()
    bridge._active_agent_id = "agent-x"
    bridge._active_run_id_str = "1"

    completed = []

    async def _fake_complete(run_id, turns):
        completed.append((run_id, turns))

    async def _fake_msg(*a, **k):
        return None

    bridge._complete = _fake_complete
    from app.crud import agent_run as crud_agent_run
    monkeypatch.setattr(crud_agent_run, "create_message", _fake_msg)

    run = types.SimpleNamespace(id=1, case_id=case.id, status="running")
    ok = await bridge._on_breakpoint_head_ready(run, 5)
    assert ok is True
    assert completed == [(1, 5)]
    # skip_initial_goto 透传
    assert calls and calls[0].get("skip_initial_goto") is True
    # 尾段步骤入覆盖 + 回放行
    assert {3, 4} <= bridge._journal_covered
    assert len(bridge._breakpoint_replay_logs) == 2
    assert "验证码断点尾段" in bridge._breakpoint_replay_logs[0]["message"]


@pytest.mark.asyncio
async def test_head_ready_tail_failure_clears_and_continues(db, monkeypatch):
    _patch_settings(monkeypatch, exec_captcha_breakpoint=True)
    bridge, case = await _make_bridge_with_case(db, meta={"captcha_segment_start": 2})
    await bridge._load_breakpoint_state(case.id)
    bridge._run_steps = [
        {"step_order": s["step_order"], "description": s["description"]} for s in CAPTCHA_STEPS
    ]
    bridge._journal_covered = {1, 2}

    class _FakeAM:
        async def _try_run_compiled_script(self, agent_id, run_id_str, name, steps, **kw):
            return [{"success": False, "compiled_script_failed": True, "error": "boom"}]

    bridge.agent_manager = _FakeAM()
    bridge._active_agent_id = "a"
    bridge._active_run_id_str = "1"

    async def _fake_msg(*a, **k):
        return None

    from app.crud import agent_run as crud_agent_run
    monkeypatch.setattr(crud_agent_run, "create_message", _fake_msg)

    run = types.SimpleNamespace(id=1, case_id=case.id, status="running")
    ok = await bridge._on_breakpoint_head_ready(run, 5)
    assert ok is False
    assert bridge._breakpoint_until is None  # 断点关闭，续 OTA
    await db.refresh(case)
    assert case.compiled_script_meta is None and case.compiled_script is None  # 坏尾段已清


@pytest.mark.asyncio
async def test_replay_skips_when_breakpoint_armed(db, monkeypatch):
    from core.agent_bridge import AgentBridge

    bridge = AgentBridge.__new__(AgentBridge)
    bridge.db = db
    bridge._breakpoint_until = 2
    run = types.SimpleNamespace(id=1, case_id=1)
    ok = await bridge._try_replay_compiled_script(run, "a", "1")
    assert ok is False  # 断点武装时不做整案回放


# ── D2. 单元提前收束（should_stop）与完成信号要求 ───────────────────────


@pytest.mark.asyncio
async def test_run_unit_should_stop_after_click():
    from core.action_unit import normalize_unit, run_unit

    steps, problems = normalize_unit(
        [
            {"action": "fill", "selector": "e1", "value": "admin"},
            {"action": "click", "selector": "e2"},
            {"action": "wait", "value": "登录成功"},
        ],
        max_steps=6,
        is_valid_ref=lambda s: True,
    )
    assert not problems
    executed = []
    stop_after = {"n": 0}

    async def _exec(action):
        executed.append(action.get("action"))
        return {"success": True}

    def _should_stop():
        # 点击之后收束
        return bool(executed) and executed[-1] == "click"

    outcome = await run_unit(steps, _exec, should_stop=_should_stop)
    assert outcome.success is True
    assert outcome.stopped_early is True
    assert executed == ["fill", "click"]  # wait 未执行


@pytest.mark.asyncio
async def test_run_unit_should_stop_false_runs_all():
    from core.action_unit import normalize_unit, run_unit

    steps, _ = normalize_unit(
        [{"action": "fill", "selector": "e1", "value": "a"}, {"action": "click", "selector": "e2"}],
        max_steps=6,
        is_valid_ref=lambda s: True,
    )
    executed = []

    async def _exec(action):
        executed.append(action.get("action"))
        return {"success": True}

    outcome = await run_unit(steps, _exec, should_stop=lambda: False)
    assert outcome.success and outcome.stopped_early is False
    assert executed == ["fill", "click"]


@pytest.mark.asyncio
async def test_maybe_engine_done_requires_completion_signal(db, monkeypatch):
    """仅 fill 覆盖头段（无完成信号）→ 不触发断点回放。"""
    _patch_settings(monkeypatch, exec_captcha_breakpoint=True)
    bridge, case = await _make_bridge_with_case(db, meta={"captcha_segment_start": 2})
    await bridge._load_breakpoint_state(case.id)
    bridge._run_steps = [
        {"step_order": s["step_order"], "description": s["description"]} for s in CAPTCHA_STEPS
    ]
    bridge._journal_covered = {1, 2}
    bridge._journal = []

    called = {"n": 0}

    async def _fake_head_ready(run, turn):
        called["n"] += 1
        return True

    bridge._on_breakpoint_head_ready = _fake_head_ready
    run = types.SimpleNamespace(id=1, case_id=case.id, status="running")

    # 末动作 fill（无 stopped_early）→ 不触发
    ok = await bridge._maybe_engine_done(
        run, 3, bridge._run_steps, {"success": True, "last_action": "fill", "executed": 2},
    )
    assert ok is False and called["n"] == 0

    # stopped_early（提交类动作提前收束）→ 触发
    ok = await bridge._maybe_engine_done(
        run, 4, bridge._run_steps,
        {"success": True, "last_action": "click", "stopped_early": True, "executed": 2},
    )
    assert ok is True and called["n"] == 1

    # 末动作 wait（断言类）→ 触发
    called["n"] = 0
    ok = await bridge._maybe_engine_done(
        run, 5, bridge._run_steps, {"success": True, "last_action": "wait", "executed": 2},
    )
    assert ok is True and called["n"] == 1


# ── E. manager payload：skip_initial_goto ────────────────────────────────

@pytest.mark.asyncio
async def test_manager_payload_skip_initial_goto(monkeypatch):
    from agent.manager import AgentManager
    from agent.models import AgentStatus

    captured = {}

    class _FakeSession:
        def __init__(self):
            self.agent = types.SimpleNamespace(status=None)

        async def request(self, msg, timeout=0):
            captured["payload"] = dict(msg.payload or {})
            return {"success": True, "steps": [{"success": True}]}

    mgr = AgentManager.__new__(AgentManager)
    mgr._last_console_errors = []

    async def _get_session(agent_id):
        return _FakeSession()

    mgr.get_session = _get_session  # type: ignore[assignment]

    res = await mgr._try_run_compiled_script(
        "agent-1", "run-1", "case",
        [{"step_order": 1, "id": 1, "description": "x"}],
        script="print(1)",
        base_url="http://x", case_id=1, steps_hash="h",
        headless=None, keep_browser=True, skip_initial_goto=True,
    )
    assert res and captured["payload"].get("skip_initial_goto") is True

    # 默认 False
    captured.clear()
    await mgr._try_run_compiled_script(
        "agent-1", "run-1", "case",
        [{"step_order": 1, "id": 1, "description": "x"}],
        script="print(1)",
        base_url="http://x", case_id=1, steps_hash="h",
    )
    assert captured["payload"].get("skip_initial_goto") is False
