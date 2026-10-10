"""034 实测发现回归：think 输出 thinking=null 时主循环崩溃（TypeError: NoneType not subscriptable）。

线上实例（2026-10-10，case 670 / 批次 467）：
  File "core/agent_runner/runner.py", line 693, in run
      logger.info("Thinking: %s", thinking_text[:120])
  TypeError: 'NoneType' object is not subscriptable
→ 整轮 run 以 status=error, turns=0 结束（真实断言失败被吞掉）。
修复：thinking_text = action.get("thinking") or ""
"""
import pytest

from core.agent_runner.runner import AgentRunner


@pytest.mark.asyncio
async def test_run_survives_none_thinking(monkeypatch):
    runner = AgentRunner(
        None, "断言页面文本",
        None,
        case_steps=[{"step_order": 1, "description": "断言页面包含【VoyanTest】", "parsed_result": ""}],
    )

    async def fake_observe(force_screenshot=False):
        return {"snapshot": "- heading \"VoyanTest\"", "url": "http://localhost:8002/login"}

    async def fake_think(observation):
        # 模型输出显式 thinking: null（实测触发形态）
        return {"thinking": None, "action": "done", "value": "断言已完成", "selector": None}

    monkeypatch.setattr(runner, "observe", fake_observe)
    monkeypatch.setattr(runner, "think", fake_think)

    result = await runner.run()
    assert isinstance(result, dict)
    # 断言文本在快照中 → 快照自动覆盖 → done 通过（同时验证门禁不误伤）
    assert result.get("status") == "completed"
    assert runner.turns_used >= 1


@pytest.mark.asyncio
async def test_done_refused_when_step_uncovered(monkeypatch):
    """034 实测假通过回归：断言未覆盖时空口 done 必须被拒绝（而非直接 passed）。"""
    runner = AgentRunner(
        None, "断言页面文本",
        None,
        case_steps=[{
            "step_order": 1,
            "description": "断言页面包含【这段文字绝对不存在-034校验】",
            "parsed_result": "",
        }],
    )

    async def fake_observe(force_screenshot=False):
        return {"snapshot": "- heading \"VoyanTest\"", "url": "http://localhost:8002/login"}

    async def fake_think(observation):
        return {"thinking": "页面不含该文本，判定失败，任务执行完毕", "action": "done", "value": "完毕"}

    monkeypatch.setattr(runner, "observe", fake_observe)
    monkeypatch.setattr(runner, "think", fake_think)

    result = await runner.run()
    assert result.get("status") == "failed"
    assert "done 被拒" in (result.get("error") or "")


@pytest.mark.asyncio
async def test_run_survives_missing_thinking(monkeypatch):
    runner = AgentRunner(None, "g", None)

    async def fake_observe(force_screenshot=False):
        return {"snapshot": "- body", "url": "http://x"}

    async def fake_think(observation):
        return {"action": "done", "value": "ok"}

    monkeypatch.setattr(runner, "observe", fake_observe)
    monkeypatch.setattr(runner, "think", fake_think)

    result = await runner.run()
    assert result.get("status") == "completed"
