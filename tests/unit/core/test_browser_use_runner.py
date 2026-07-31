"""Unit tests for browser-use execution backend (scheme B)."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.browser_use_exec import _build_step_task, _history_to_step_fields


class TestBuildStepTask:
    def test_includes_expected(self):
        task = _build_step_task(
            description="点击登录",
            expected_result="进入首页",
            step_order=1,
            base_url="https://example.com",
        )
        assert "点击登录" in task
        assert "进入首页" in task
        # 不得把真实 URL 写入 task，否则 browser-use 会自动 navigate 刷掉前序 UI 状态
        assert "https://example.com" not in task
        assert "BASE URL" not in task
        assert "禁止重新打开" in task or "禁止 navigate" in task
        assert "步骤编号: 1" in task

    def test_empty_expected(self):
        task = _build_step_task(
            description="点击登录",
            expected_result=None,
            step_order=2,
            base_url=None,
        )
        assert "未写明" in task
        assert "BASE URL" not in task
        assert "http://" not in task


class TestHistoryToStepFields:
    def test_success_true(self):
        hist = MagicMock()
        hist.is_successful.return_value = True
        hist.model_thoughts.return_value = ["think"]
        hist.action_names.return_value = ["click", "done"]
        hist.final_result.return_value = "ok"
        hist.judgement.return_value = None
        fields = _history_to_step_fields(hist)
        assert fields["success"] is True
        assert fields["error"] is None
        assert "click" in fields["action"]

    def test_success_false(self):
        hist = MagicMock()
        hist.is_successful.return_value = False
        hist.model_thoughts.return_value = []
        hist.action_names.return_value = []
        hist.final_result.return_value = "找不到按钮"
        hist.judgement.return_value = None
        fields = _history_to_step_fields(hist)
        assert fields["success"] is False
        assert "找不到按钮" in fields["error"]

    def test_success_none_treated_as_fail(self):
        hist = MagicMock()
        hist.is_successful.return_value = None
        hist.model_thoughts.return_value = []
        hist.action_names.return_value = []
        hist.final_result.return_value = None
        hist.judgement.return_value = None
        fields = _history_to_step_fields(hist)
        assert fields["success"] is False

    def test_judge_overrides_agent_false_negative(self):
        hist = MagicMock()
        hist.is_successful.return_value = False
        hist.model_thoughts.return_value = []
        hist.action_names.return_value = ["input", "click", "done"]
        hist.final_result.return_value = "无法完成：筛选框不可见"
        hist.judgement.return_value = {
            "verdict": False,
            "failure_reason": (
                "The agent incorrectly reported task failure despite successfully "
                "completing all required steps. Screenshots clearly show the unit was selected."
            ),
            "reasoning": "UI shows 京州市院 in the unit field.",
        }
        fields = _history_to_step_fields(hist)
        assert fields["success"] is True
        assert fields["error"] is None
        assert "judge override" in fields["thinking"]

    def test_judge_verdict_true_overrides_agent_fail(self):
        hist = MagicMock()
        hist.is_successful.return_value = False
        hist.model_thoughts.return_value = []
        hist.action_names.return_value = ["done"]
        hist.final_result.return_value = "agent said fail"
        hist.judgement.return_value = {
            "verdict": True,
            "failure_reason": "",
            "reasoning": "Task completed.",
        }
        fields = _history_to_step_fields(hist)
        assert fields["success"] is True
        assert fields["error"] is None


@pytest.mark.asyncio
async def test_run_test_case_routes_to_browser_use():
    from core.runner import _orchestrator as orch

    with patch.object(orch, "AsyncSessionLocal") as mock_session_cls:
        # project lock path: no project → unlocked
        session = AsyncMock()
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=None)
        mock_session_cls.return_value = session

        with (
            patch("app.crud.get_test_case", new=AsyncMock(return_value=None)),
            patch(
                "core.browser_use_runner.run_test_case_via_browser_use",
                new=AsyncMock(return_value={"case_id": 1, "status": "passed", "backend": "browser_use"}),
            ) as mock_bu,
            patch("app.runtime_config.execution_backend_config") as cfg,
        ):
            cfg.backend = "playwright_mcp"
            cfg.max_steps_per_nl = 20
            cfg.headless = True
            result = await orch.run_test_case(1, backend="browser_use")
            mock_bu.assert_awaited_once()
            assert result["backend"] == "browser_use"
