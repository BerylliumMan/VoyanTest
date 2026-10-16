"""执行引擎工具调用落库的健壮性（2026-10-10 实测缺陷回归）。

背景：服务端 OTA 单动作路径把 LLM 决策记录为 agent_tool_calls 时，
``action.get("name", action.get("action", "unknown"))`` 在键存在但值为 None 时
返回 None → tool_name NOT NULL 违约 → 整条 run 以异常结束且卡在 pending
（批次 481 / run 735 实例）；同时会话被污染导致 mark_run_failed 连锁失败。
"""
import pytest

from core.agent_runner import runner as runner_mod


@pytest.mark.asyncio
async def test_record_turn_coerces_missing_tool_name(monkeypatch):
    captured = {}

    async def fake_create_message(db, run_id, turn, role, content):
        return None

    async def fake_create_tool_call(db, run_id, turn, tool_name, tool_args, success, error):
        captured["tool_name"] = tool_name
        captured["tool_args"] = tool_args

    monkeypatch.setattr(runner_mod.crud_agent_run, "create_message", fake_create_message)
    monkeypatch.setattr(runner_mod.crud_agent_run, "create_tool_call", fake_create_tool_call)

    r = runner_mod.AgentRunner.__new__(runner_mod.AgentRunner)
    r._db = object()
    r._run_id = 1

    # name/action 键存在但为 None（实测形态："未知操作: None"）
    await r._record_turn(
        5, "snapshot",
        {"name": None, "action": None, "snapshot_version": "abc"},
        {"success": False, "error": "未知操作: None"},
    )
    assert captured["tool_name"] == "unknown"
    assert captured["tool_args"] == {"snapshot_version": "abc"}

    # 动作整体缺失（None）也不允许崩
    await r._record_turn(6, "snapshot", None, None)
    assert captured["tool_name"] == "unknown"

    # 正常动作名不受影响
    await r._record_turn(7, "snapshot", {"action": "click", "selector": "e1"}, {"success": True})
    assert captured["tool_name"] == "click"


@pytest.mark.asyncio
async def test_record_turn_merges_fetch_api_and_evidence(monkeypatch):
    """036：fetch 的 api 元数据与证据路径并入 tool_args（报告标注/审计用）。"""
    captured = {}

    async def fake_create_message(db, run_id, turn, role, content):
        return None

    async def fake_create_tool_call(db, run_id, turn, tool_name, tool_args, success, error):
        captured["tool_name"] = tool_name
        captured["tool_args"] = tool_args

    monkeypatch.setattr(runner_mod.crud_agent_run, "create_message", fake_create_message)
    monkeypatch.setattr(runner_mod.crud_agent_run, "create_tool_call", fake_create_tool_call)

    r = runner_mod.AgentRunner.__new__(runner_mod.AgentRunner)
    r._db = object()
    r._run_id = 1

    await r._record_turn(
        8, "snapshot",
        {"action": "fetch", "value": '{"method":"POST","url":"/api/export"}'},
        {
            "success": True,
            "fetch": {
                "api": {"method": "POST", "url": "/api/export", "status": 200,
                        "size": 51234, "content_type": "text/csv", "excerpt": "a,b\nc,d"},
                "evidence_path": "reports/agent_1/evidence/export.csv",
                "evidence_size": 51234,
                "evidence_truncated": False,
            },
        },
    )
    assert captured["tool_name"] == "fetch"
    assert captured["tool_args"]["api"]["status"] == 200
    assert captured["tool_args"]["api"]["excerpt"].startswith("a,b")
    assert captured["tool_args"]["evidence_path"] == "reports/agent_1/evidence/export.csv"
    assert captured["tool_args"]["evidence_truncated"] is False


def test_persist_fetch_evidence_writes_file(tmp_path, monkeypatch):
    """036：runner 证据落盘（bytes → 文件；fetch 字典获得路径字段）。"""
    monkeypatch.chdir(tmp_path)
    r = runner_mod.AgentRunner.__new__(runner_mod.AgentRunner)
    r._evidence_dir = "reports/agent_9/evidence"

    result = {
        "success": True,
        "fetch": {
            "api": {"content_type": "text/csv"},
            "filename": "out.csv",
            "evidence": {"data": b"x,y\n", "size": 4, "truncated": False},
        },
    }
    r._persist_fetch_evidence(result)
    fetch = result["fetch"]
    assert "evidence" not in fetch
    assert fetch["evidence_path"] == "reports/agent_9/evidence/out.csv"
    assert (tmp_path / "reports/agent_9/evidence/out.csv").read_bytes() == b"x,y\n"

    # 无证据目录：如实标注，不崩
    r2 = runner_mod.AgentRunner.__new__(runner_mod.AgentRunner)
    r2._evidence_dir = None
    result2 = {"fetch": {"api": {}, "evidence": {"data": b"1", "size": 1}}}
    r2._persist_fetch_evidence(result2)
    assert "evidence_error" in result2["fetch"]


def test_runner_actions_include_verify_and_fetch():
    """036 回归：verify 四件套（B5 漏接服务端注册表）+ fetch 必须在 _RUNNER_ACTIONS。"""
    for name in ("verify_text", "verify_visible", "verify_value", "verify_list", "fetch"):
        assert name in runner_mod._RUNNER_ACTIONS, name


@pytest.mark.asyncio
async def test_act_or_unit_routes_unit_through_unit_executor():
    """036 回归：恢复路径的 unit 决策必须走宏动作单元（此前直发 act → 未知操作: None）。"""
    r = runner_mod.AgentRunner.__new__(runner_mod.AgentRunner)
    calls = []

    async def fake_unit(turn, unit_raw, observation):
        calls.append(("unit", turn, len(unit_raw)))
        return {"success": True}

    async def fake_act(action):
        calls.append(("act", action.get("action")))
        return {"success": True}

    r._execute_action_unit = fake_unit
    r.act = fake_act

    out = await r._act_or_unit({"action": None, "unit": [{"action": "click"}]}, 7, {})
    assert out["success"] is True
    assert calls == [("unit", 7, 1)]

    out2 = await r._act_or_unit({"action": "click"}, 8, {})
    assert out2["success"] is True
    assert calls[-1] == ("act", "click")
