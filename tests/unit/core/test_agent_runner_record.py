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
