"""2026-09-30 修复的三处（客户端跑用例验证暴露）：批量路由 / 固化合成 / tool_args 落库。"""
from __future__ import annotations

import pytest


# ── 036：fetch 的 value 常被模型写成 JSON 对象（pydantic 曾整条作废）──────────


def test_fetch_value_dict_is_serialized():
    import json as _json

    from core.llm_wrapper import PlaywrightMCPToolCall

    tc = PlaywrightMCPToolCall.model_validate({
        "action": "fetch",
        "value": {"method": "GET", "url": "/api/export.csv", "evidence": True},
    })
    assert isinstance(tc.value, str)
    parsed = _json.loads(tc.value)
    assert parsed["method"] == "GET"
    assert parsed["evidence"] is True


def test_wait_value_int_still_ok():
    from core.llm_wrapper import PlaywrightMCPToolCall

    tc = PlaywrightMCPToolCall.model_validate({"action": "wait", "value": 1})
    assert str(tc.value) == "1"


# ── ① 批量带 agent_name → 路由到客户端派发 ─────────────────────────────────


@pytest.mark.asyncio
async def test_batch_run_routes_to_client_when_agent_name(monkeypatch):
    from app.routers.testcase.execution import _server as srv
    from app.routers.testcase.execution._schemas import BatchRunRequest

    called: dict = {}

    async def fake_batch_run_client(body, user=None, db=None):
        called["body"] = body
        return {"batch_id": 999, "backend": "ota", "message": "routed-to-client"}

    monkeypatch.setattr(
        "app.routers.testcase.execution._client.batch_run_client", fake_batch_run_client
    )
    out = await srv.batch_run_cases(
        BatchRunRequest(case_ids=[1, 2], environment_id=17, agent_name="客户端A"),
        background_tasks=None,
        user=type("U", (), {"id": 1, "username": "admin", "role": "admin"})(),
        db=None,
    )
    assert out["batch_id"] == 999
    assert called["body"].agent_name == "客户端A"
    assert called["body"].case_ids == [1, 2]
    assert called["body"].environment_id == 17


# ── ② AgentRunner 的 journal 与 tool_args ────────────────────────────────


def test_runner_collects_journal_entry():
    from core.agent_runner.runner import AgentRunner

    runner = AgentRunner.__new__(AgentRunner)  # 只测纯方法，绕过依赖
    runner.journal = []  # type: ignore[attr-defined]
    runner._collect_journal_entry(  # type: ignore[attr-defined]
        {"action": "fill", "selector": "e11", "value": "standard_user", "target": "Username"},
        3,
    )
    assert runner.journal and runner.journal[0]["action"] == "fill"
    assert runner.journal[0]["selector"] == "e11"
    assert runner.journal[0]["value"] == "standard_user"
    assert runner.journal[0]["element_desc"] == "Username"
    # 无动作名 → 不入 journal（防御）
    runner._collect_journal_entry({"action": ""}, 4)
    assert len(runner.journal) == 1


@pytest.mark.asyncio
async def test_record_turn_flattens_top_level_args(monkeypatch):
    """tool_args 曾恒为 {}（LLM 把参数放顶层而非 args）——修复后应落库真实定位符与取值。"""
    from app.crud import agent_run as crud_agent_run
    from core.agent_runner.runner import AgentRunner

    captured: dict = {}

    async def fake_create_tool_call(db, run_id, turn, tool_name, tool_args, success, error):
        captured.update(
            tool_name=tool_name, tool_args=tool_args, success=success, turn=turn
        )
        return None

    async def fake_create_message(*_a, **_k):
        return None

    monkeypatch.setattr(crud_agent_run, "create_tool_call", fake_create_tool_call)
    monkeypatch.setattr(crud_agent_run, "create_message", fake_create_message)

    runner = AgentRunner.__new__(AgentRunner)
    runner._db = object()  # type: ignore[attr-defined]
    runner._run_id = 7  # type: ignore[attr-defined]
    await runner._record_turn(  # type: ignore[attr-defined]
        2, "[snapshot]", {"action": "fill", "selector": "e11", "value": "abc"}, {"success": True}
    )
    assert captured["tool_args"]["selector"] == "e11"
    assert captured["tool_args"]["value"] == "abc"
    assert captured["tool_name"] == "fill" and captured["success"] is True


# ── ③ 跑成功 → 固化脚本（服务端 OTA）─────────────────────────────────────


@pytest.mark.asyncio
async def test_synthesize_and_persist_writes_fields(db, monkeypatch):
    from app import db_models
    from core.runner import _orchestrator as orch

    proj = db_models.Project(name="ota-fix", base_url="https://e.com")
    db.add(proj)
    await db.commit()
    await db.refresh(proj)
    case = db_models.TestCase(project_id=proj.id, name="固化用例", priority="high", case_kind="functional")
    db.add(case)
    await db.commit()
    await db.refresh(case)

    async def fake_synthesize(**kwargs):
        return "from playwright.sync_api import Page\n\ndef test_x(page: Page):\n    page.goto('https://e.com')\n"

    monkeypatch.setattr("core.script_synthesize.synthesize_playwright_script", fake_synthesize)

    await orch._synthesize_and_persist_compiled_script(
        db=db, tc=case, case_id=case.id,
        steps=[type("S", (), {"step_order": 1, "description": "打开页面", "parsed_result": ""})()],
        goal_text="执行测试用例", journal=[{"action": "goto", "value": "https://e.com"}],
        llm_client=object(), model="m", base_url="https://e.com",
    )
    await db.refresh(case)
    assert case.compiled_script and "playwright" in case.compiled_script
    assert case.compiled_script_hash
    assert case.compiled_at is not None


@pytest.mark.asyncio
async def test_synthesize_skips_when_no_journal(db, monkeypatch):
    """无成功动作日志 → 不合成、不改字段（且不抛错）。"""
    from app import db_models
    from core.runner import _orchestrator as orch

    proj = db_models.Project(name="ota-fix2", base_url="https://e.com")
    db.add(proj)
    await db.commit()
    await db.refresh(proj)
    case = db_models.TestCase(project_id=proj.id, name="空日志", priority="high", case_kind="functional")
    db.add(case)
    await db.commit()
    await db.refresh(case)

    await orch._synthesize_and_persist_compiled_script(
        db=db, tc=case, case_id=case.id, steps=[], goal_text="g", journal=[],
        llm_client=object(), model="m", base_url=None,
    )
    await db.refresh(case)
    assert case.compiled_script is None


# ── ④ 断言步骤的确定性覆盖 + 拒绝 done 上限（2026-09-30 客户端 519 死循环）──


def test_assertion_expectation_extraction():
    from core.agent_bridge import AgentBridge

    assert AgentBridge._assertion_expectation("断言页面包含【Products】") == "Products"
    assert (
        AgentBridge._assertion_expectation("断言页面包含【Test.allTheThings() T-Shirt (Red)】")
        == "Test.allTheThings() T-Shirt (Red)"
    )
    assert AgentBridge._assertion_expectation("验证列表显示「Your Cart」") == "Your Cart"
    # 无括号：剥掉动词/谓语后的剩余文本
    assert AgentBridge._assertion_expectation("断言页面显示 6 件商品") == "6 件商品"


def test_cover_assertion_steps_from_snapshot():
    from core.agent_bridge import AgentBridge

    bridge = AgentBridge.__new__(AgentBridge)
    bridge._run_steps = [  # type: ignore[attr-defined]
        {"step_order": 4, "description": "在【Product Sort By】中选择「Name (Z to A)」"},
        {"step_order": 5, "description": "断言页面包含【Test.allTheThings() T-Shirt (Red)】"},
        {"step_order": 6, "description": "断言页面包含【不存在的商品】"},
    ]
    bridge._journal_covered = {1, 2, 3, 4}  # type: ignore[attr-defined]
    snapshot = (
        "### Page\n- Page URL: https://www.saucedemo.com/inventory.html\n"
        "```yaml\n- generic [ref=e1]: Test.allTheThings() T-Shirt (Red)\n- generic: Sauce Labs Backpack\n```"
    )
    covered = bridge._cover_assertion_steps_from_snapshot([5, 6], snapshot)  # type: ignore[attr-defined]
    assert covered == [5]  # 5 的期望文本在快照里 → 覆盖；6 不在 → 不覆盖
    assert 5 in bridge._journal_covered  # type: ignore[attr-defined]
    # 非断言步骤不参与自动覆盖
    assert bridge._cover_assertion_steps_from_snapshot([4], snapshot) == []  # type: ignore[attr-defined]


def test_assertion_cover_requires_exact_text():
    """不放水：期望文本不在快照里 → 绝不覆盖。"""
    from core.agent_bridge import AgentBridge

    bridge = AgentBridge.__new__(AgentBridge)
    bridge._run_steps = [{"step_order": 5, "description": "断言页面包含【Test.allTheThings() T-Shirt (Red)】"}]  # type: ignore[attr-defined]
    bridge._journal_covered = set()  # type: ignore[attr-defined]
    assert bridge._cover_assertion_steps_from_snapshot([5], "- text: T-Shirt (Red)") == []  # type: ignore[attr-defined]

    # parsed_result 兜底（描述没写括号时）
    bridge._run_steps = [{"step_order": 6, "description": "断言排序正确", "parsed_result": "Test.allTheThings() T-Shirt (Red)"}]  # type: ignore[attr-defined]
    assert bridge._cover_assertion_steps_from_snapshot([6], "x Test.allTheThings() T-Shirt (Red) y") == [6]  # type: ignore[attr-defined]


def test_refuse_done_limit_constant():
    from core import agent_bridge

    assert agent_bridge._REFUSE_DONE_LIMIT == 5


def test_short_expectation_boundary_match():
    """短期望（徽标数字「1」）按词边界匹配：命中独立 token，不命中 "134" 这类子串。"""
    from core.agent_bridge import AgentBridge

    bridge = AgentBridge.__new__(AgentBridge)
    bridge._run_steps = [  # type: ignore[attr-defined]
        {"step_order": 3, "description": "断言购物车图标徽标数字为【1】"},
    ]
    bridge._journal_covered = set()  # type: ignore[attr-defined]
    # 独立 token "1" → 覆盖
    assert bridge._cover_assertion_steps_from_snapshot([3], '- generic [ref=e5]: "1"') == [3]  # type: ignore[attr-defined]
    # 只在 "134"/"item-11" 里出现 → 不覆盖
    bridge._journal_covered = set()  # type: ignore[attr-defined]
    assert bridge._cover_assertion_steps_from_snapshot([3], "- text: 134 items item-11") == []  # type: ignore[attr-defined]


# ── ⑤ 客户端执行模式标识 + 断线重连 ────────────────────────────────────────


def test_client_execution_mode_label():
    from core.agent_bridge import AgentBridge

    bridge = AgentBridge.__new__(AgentBridge)
    bridge._active_agent_id = "本机客户端-验证"  # type: ignore[attr-defined]
    assert bridge._client_execution_mode() == "client:本机客户端-验证"  # type: ignore[attr-defined]
    bridge._active_agent_id = None  # type: ignore[attr-defined]
    assert bridge._client_execution_mode() == "client:unknown"  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_client_run_forever_reconnects(monkeypatch):
    """服务端断开后自动重连（此前客户端跑一次就退出）。"""
    import agent.client_core as cc

    attempts: list[int] = []

    class _FakeClient:
        _stopped = False

        async def start(self):
            attempts.append(1)
            if len(attempts) >= 3:
                self._stopped = True  # 第三次连接后主动停止，结束循环
                return
            raise ConnectionError("Connection closed by server")

        def _emit_status(self, *_a):
            pass

        def _log_info(self, *_a):
            pass

    slept: list[float] = []

    async def fake_sleep(sec):
        slept.append(sec)

    monkeypatch.setattr(cc.asyncio, "sleep", fake_sleep)
    fake = _FakeClient()
    # 直接调用类方法（self 用假对象）
    await cc.AgentClient.run_forever(fake)
    assert len(attempts) == 3            # 连挂两次后重试到第三次
    assert slept[:2] == [3.0, 6.0]       # 指数退避 3 → 6
