"""Tests for core/runner/_execution.py — run_test_case_in_browser.

Strategy: real SQLite DB via the ``db`` fixture, mock only the external
PlaywrightMCPManager + execute_step_mcp + LLM boundaries. This mirrors the
E2E style already used in tests/test_runner.py::TestRunTestCaseInBrowserE2E
but covers the additional code paths that those tests do not exercise:
- missing case / project
- empty step list
- injected auth cookies + base_url_override
- navigation failure
- pre-existing run_id (mark_run_running path)
- step retry on failure (without self-heal)
- self-heal triggers + persists to DB
- debug mode decisions: retry / skip / edit / abort
- per-step assertion verification (pass + fail + exception)
- consecutive-failure early exit
- outer exception handler (test_status = failed)
- save_run_results path when no run_id
- own DB session close
"""
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.exc import SQLAlchemyError

from core.runner._execution import run_test_case_in_browser


# ---------------------------------------------------------------------------
# Local helpers (mirrors tests/test_runner.py)
# ---------------------------------------------------------------------------


def _executor_result(
    success: bool,
    step_number: int = 1,
    error: str | None = None,
    action: str = "click",
) -> dict:
    return {
        "step_number": step_number,
        "original_description": f"step {step_number}",
        "success": success,
        "status": "passed" if success else "failed",
        "thinking": "",
        "action": action if success else "",
        "next_goal": "",
        "error": error,
        "screenshot_path": None,
        "duration_ms": 100,
    }


def _create_case_with_steps(db, project_id, name, step_count=2, *,
                             retry_max=0, retry_delay=1.0,
                             assertions=None, descriptions=None):
    from app import db_models
    case = db_models.TestCase(
        project_id=project_id, name=name, description="", steps=[],
    )
    db.add(case)
    db.flush()
    for i in range(1, step_count + 1):
        desc = (descriptions or {}).get(i, f"step {i}")
        step = db_models.TestStep(
            case_id=case.id,
            step_order=i,
            description=desc,
            retry_max=retry_max,
            retry_delay=retry_delay,
            assertions=assertions or [],
        )
        db.add(step)
    db.commit()
    db.refresh(case)
    return case.id


def _create_batch(db, project_id, total_cases=1):
    from app import db_models
    batch = db_models.RunBatch(project_id=project_id, total_cases=total_cases)
    db.add(batch)
    db.commit()
    db.refresh(batch)
    return batch.id


def _create_pending_run(db, case_id, batch_id):
    from app import db_models
    run = db_models.TestRun(
        case_id=case_id, batch_id=batch_id, status="pending",
        start_time=None, end_time=None,
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    return run.id


def _wire_mcp():
    mcp = AsyncMock()
    mcp.call_tool = AsyncMock(return_value={"success": True})
    mcp.clear_cookies = AsyncMock()
    return mcp


# ---------------------------------------------------------------------------
# Default patches context manager
# ---------------------------------------------------------------------------


class _DefaultPatches:
    """Wraps the standard 5 patches as a single context manager.

    Exposes ``crud`` and ``executor`` for tests that need to tweak behaviour.
    """

    def __enter__(self):
        self._stack = ExitStack()
        self.crud = self._stack.enter_context(
            patch("core.runner._execution.crud")
        )
        self.executor = self._stack.enter_context(
            patch("core.runner._execution.execute_step_mcp",
                  new=AsyncMock(return_value=_executor_result(True, 1)))
        )
        self._stack.enter_context(patch("core.runner._execution.create_openai_client"))
        self._stack.enter_context(
            patch("core.runner._execution._resolve_llm_config",
                  return_value=(None, None, "gpt-4"))
        )
        self._stack.enter_context(
            patch("core.runner._execution._resolve_env_cookies", return_value=[])
        )
        return self

    def __exit__(self, *exc):
        return self._stack.__exit__(*exc)


class _DecisionDict(dict):
    """Dict-like that returns a default decision for any key.

    Used to mock ``_pause_decisions`` so we don't have to predict the
    run_id that ``run_test_case_in_browser`` will allocate.
    """

    def __init__(self, decision: str, **default_fields):
        super().__init__()
        self._decision = decision
        self._defaults = {"decision": decision, **default_fields}

    def get(self, key, default=None):
        if key in self:
            return dict.__getitem__(self, key)
        return dict(self._defaults)

    def pop(self, key, *args):
        return dict.pop(self, key, *args)


def _wire_crud(mock_crud, db, case_id, project_id):
    from app import db_models
    case_row = db.query(db_models.TestCase).filter(
        db_models.TestCase.id == case_id
    ).first()
    project_row = db.query(db_models.Project).filter(
        db_models.Project.id == project_id
    ).first()
    mock_crud.get_test_case.return_value = case_row
    mock_crud.get_project.return_value = project_row
    mock_crud.get_steps_for_case.return_value = (
        db.query(db_models.TestStep)
        .filter(db_models.TestStep.case_id == case_id)
        .order_by(db_models.TestStep.step_order)
        .all()
    )


# ---------------------------------------------------------------------------
# Negative paths
# ---------------------------------------------------------------------------


class TestRunTestCaseInBrowserNegativePaths:
    @pytest.mark.asyncio
    async def test_case_not_found_returns_failed(self, db, sample_project):
        """case 不存在时 outer except 捕获并返回 failed status。"""
        mcp = _wire_mcp()
        with (
            patch("core.runner._execution.crud") as mock_crud,
            patch("core.runner._execution.create_openai_client"),
            patch("core.runner._execution._resolve_llm_config",
                  return_value=(None, None, "gpt-4")),
            patch("core.runner._execution._resolve_env_cookies", return_value=[]),
        ):
            mock_crud.get_test_case.return_value = None
            mock_crud.get_project.return_value = None
            result = await run_test_case_in_browser(999999, mcp, db=db)

        assert result["status"] == "failed"

    @pytest.mark.asyncio
    async def test_project_not_found_returns_failed(self, db, sample_project):
        from app import db_models
        case = db_models.TestCase(
            project_id=999999, name="orphan", description="", steps=[],
        )
        db.add(case)
        db.commit()
        db.refresh(case)

        mcp = _wire_mcp()
        with (
            patch("core.runner._execution.crud") as mock_crud,
            patch("core.runner._execution.create_openai_client"),
            patch("core.runner._execution._resolve_llm_config",
                  return_value=(None, None, "gpt-4")),
            patch("core.runner._execution._resolve_env_cookies", return_value=[]),
        ):
            case_row = db.query(db_models.TestCase).filter(
                db_models.TestCase.id == case.id
            ).first()
            mock_crud.get_test_case.return_value = case_row
            mock_crud.get_project.return_value = None
            result = await run_test_case_in_browser(case.id, mcp, db=db)

        assert result["status"] == "failed"

    @pytest.mark.asyncio
    async def test_no_steps_returns_failed(self, db, sample_project):
        from app import db_models
        case = db_models.TestCase(
            project_id=sample_project["id"], name="empty", description="", steps=[],
        )
        db.add(case)
        db.commit()
        db.refresh(case)

        mcp = _wire_mcp()
        with _DefaultPatches() as patches:
            _wire_crud(patches.crud, db, case.id, sample_project["id"])
            patches.crud.get_steps_for_case.return_value = []
            result = await run_test_case_in_browser(case.id, mcp, db=db)

        assert result["status"] == "failed"


# ---------------------------------------------------------------------------
# Base URL override + auth cookies
# ---------------------------------------------------------------------------


class TestRunTestCaseInBrowserBaseUrl:
    @pytest.mark.asyncio
    async def test_base_url_override_used_for_navigation(
        self, db, sample_project
    ):
        case_id = _create_case_with_steps(db, sample_project["id"], "nav-case", 1)
        mcp = _wire_mcp()
        batch_id = _create_batch(db, sample_project["id"])
        with _DefaultPatches() as patches:
            _wire_crud(patches.crud, db, case_id, sample_project["id"])
            await run_test_case_in_browser(
                case_id, mcp, db=db, batch_id=batch_id,
                base_url_override="https://override.example.com",
            )
        nav_calls = [
            c for c in mcp.call_tool.await_args_list
            if c.args and c.args[0] == "browser_navigate"
        ]
        assert any("override.example.com" in c.args[1].get("url", "")
                   for c in nav_calls)

    @pytest.mark.asyncio
    async def test_injects_auth_cookies_when_env_has_them(
        self, db, sample_project
    ):
        case_id = _create_case_with_steps(db, sample_project["id"], "auth-cookie-case", 1)
        mcp = _wire_mcp()
        env_cookies = [{"name": "session", "value": "abc"}]
        with (
            patch("core.runner._execution.crud") as mock_crud,
            patch("core.runner._execution.execute_step_mcp",
                  new=AsyncMock(return_value=_executor_result(True, 1))),
            patch("core.runner._execution.create_openai_client"),
            patch("core.runner._execution._resolve_llm_config",
                  return_value=(None, None, "gpt-4")),
            patch("core.runner._execution._resolve_env_cookies",
                  return_value=env_cookies),
            patch("core.runner._execution._inject_auth_cookies",
                  new=AsyncMock(return_value=1)) as mock_inject,
        ):
            _wire_crud(mock_crud, db, case_id, sample_project["id"])
            batch_id = _create_batch(db, sample_project["id"])
            await run_test_case_in_browser(
                case_id, mcp, db=db, batch_id=batch_id,
                base_url_override="https://override.example.com",
            )
        mock_inject.assert_awaited_once()
        args = mock_inject.await_args
        assert args.args[1] == env_cookies
        assert args.args[2] == "https://override.example.com"

    @pytest.mark.asyncio
    async def test_navigation_failure_logged_but_continues(
        self, db, sample_project
    ):
        case_id = _create_case_with_steps(db, sample_project["id"], "nav-fail", 1)
        mcp = _wire_mcp()
        mcp.call_tool = AsyncMock(return_value={"success": False, "text": "nav error"})

        with _DefaultPatches() as patches:
            _wire_crud(patches.crud, db, case_id, sample_project["id"])
            batch_id = _create_batch(db, sample_project["id"])
            result = await run_test_case_in_browser(
                case_id, mcp, db=db, batch_id=batch_id,
            )
        assert result["status"] == "passed"


# ---------------------------------------------------------------------------
# Pre-existing run_id (mark_run_running)
# ---------------------------------------------------------------------------


class TestRunTestCaseInBrowserPreExistingRun:
    @pytest.mark.asyncio
    async def test_uses_existing_run_id(self, db, sample_project):
        case_id = _create_case_with_steps(db, sample_project["id"], "preexist", 1)
        mcp = _wire_mcp()
        batch_id = _create_batch(db, sample_project["id"])
        run_id = _create_pending_run(db, case_id, batch_id)

        with _DefaultPatches() as patches:
            _wire_crud(patches.crud, db, case_id, sample_project["id"])
            await run_test_case_in_browser(
                case_id, mcp, db=db, batch_id=batch_id, run_id=run_id,
            )

        from app import db_models
        run = db.query(db_models.TestRun).filter(
            db_models.TestRun.id == run_id
        ).first()
        assert run.status == "passed"

    @pytest.mark.asyncio
    async def test_missing_run_id_falls_back_to_create(
        self, db, sample_project
    ):
        case_id = _create_case_with_steps(db, sample_project["id"], "missing-run", 1)
        mcp = _wire_mcp()
        batch_id = _create_batch(db, sample_project["id"])

        with (
            patch("core.runner._execution.mark_run_running",
                  return_value=False) as mock_mark,
            patch("core.runner._execution.crud") as mock_crud,
            patch("core.runner._execution.execute_step_mcp",
                  new=AsyncMock(return_value=_executor_result(True, 1))),
            patch("core.runner._execution.create_openai_client"),
            patch("core.runner._execution._resolve_llm_config",
                  return_value=(None, None, "gpt-4")),
            patch("core.runner._execution._resolve_env_cookies", return_value=[]),
        ):
            _wire_crud(mock_crud, db, case_id, sample_project["id"])
            await run_test_case_in_browser(
                case_id, mcp, db=db, batch_id=batch_id, run_id=999999,
            )
        mock_mark.assert_called_once_with(db, 999999)
        from app import db_models
        runs = db.query(db_models.TestRun).filter(
            db_models.TestRun.batch_id == batch_id
        ).all()
        assert len(runs) == 1
        assert runs[0].status == "passed"


# ---------------------------------------------------------------------------
# Step retry (without self-heal)
# ---------------------------------------------------------------------------


class TestRunTestCaseInBrowserRetry:
    @pytest.mark.asyncio
    async def test_step_retry_recovers_from_transient_failure(
        self, db, sample_project
    ):
        case_id = _create_case_with_steps(
            db, sample_project["id"], "retry-ok", 1,
            retry_max=2, retry_delay=0,
        )
        mcp = _wire_mcp()
        batch_id = _create_batch(db, sample_project["id"])

        with _DefaultPatches() as patches:
            _wire_crud(patches.crud, db, case_id, sample_project["id"])
            patches.executor.side_effect = [
                _executor_result(False, 1, error="transient"),
                _executor_result(True, 1),
            ]
            result = await run_test_case_in_browser(
                case_id, mcp, db=db, batch_id=batch_id,
            )

        assert result["status"] == "passed"
        assert patches.executor.await_count == 2

    @pytest.mark.asyncio
    async def test_step_retry_exhausted_marks_failed(
        self, db, sample_project
    ):
        case_id = _create_case_with_steps(
            db, sample_project["id"], "retry-fail", 1,
            retry_max=2, retry_delay=0,
        )
        mcp = _wire_mcp()
        batch_id = _create_batch(db, sample_project["id"])

        with _DefaultPatches() as patches:
            _wire_crud(patches.crud, db, case_id, sample_project["id"])
            patches.executor.side_effect = [
                _executor_result(False, 1, error="p1"),
                _executor_result(False, 1, error="p2"),
                _executor_result(False, 1, error="p3"),
            ]
            result = await run_test_case_in_browser(
                case_id, mcp, db=db, batch_id=batch_id,
            )

        assert result["status"] == "failed"
        assert patches.executor.await_count == 3

    @pytest.mark.asyncio
    async def test_executor_exception_converted_to_failed_result(
        self, db, sample_project
    ):
        case_id = _create_case_with_steps(
            db, sample_project["id"], "exec-exc", 1,
            retry_max=0,
        )
        mcp = _wire_mcp()
        batch_id = _create_batch(db, sample_project["id"])

        with _DefaultPatches() as patches:
            _wire_crud(patches.crud, db, case_id, sample_project["id"])
            patches.executor.side_effect = RuntimeError("mcp stdio broken")
            result = await run_test_case_in_browser(
                case_id, mcp, db=db, batch_id=batch_id,
            )

        assert result["status"] == "failed"
        assert result["step_results"][0]["success"] is False
        assert result["step_results"][0]["status"] == "error"


# ---------------------------------------------------------------------------
# Self-healing path
# ---------------------------------------------------------------------------


class TestRunTestCaseInBrowserSelfHeal:
    @pytest.mark.asyncio
    async def test_healable_error_triggers_heal_and_persists(
        self, db, sample_project
    ):
        case_id = _create_case_with_steps(
            db, sample_project["id"], "heal-ok", 1,
            retry_max=1, retry_delay=0,
        )
        mcp = _wire_mcp()
        batch_id = _create_batch(db, sample_project["id"])

        with _DefaultPatches() as patches:
            _wire_crud(patches.crud, db, case_id, sample_project["id"])
            patches.executor.side_effect = [
                _executor_result(False, 1, error="element not found"),
                _executor_result(True, 1, action="click(#healed)"),
            ]
            with patch(
                "core.self_healing.try_heal_and_retry",
                new=AsyncMock(return_value="#healed"),
            ) as mock_heal:
                result = await run_test_case_in_browser(
                    case_id, mcp, db=db, batch_id=batch_id,
                )

        mock_heal.assert_awaited_once()
        assert result["status"] == "passed"

        from app import db_models
        step_row = db.query(db_models.TestStep).filter(
            db_models.TestStep.case_id == case_id
        ).first()
        assert step_row.healed_selector == "#healed"

    @pytest.mark.asyncio
    async def test_healable_persistence_db_error_logged(
        self, db, sample_project
    ):
        case_id = _create_case_with_steps(
            db, sample_project["id"], "heal-dbfail", 1,
            retry_max=1, retry_delay=0,
        )
        mcp = _wire_mcp()
        batch_id = _create_batch(db, sample_project["id"])

        original_commit = db.commit
        healing_commit_started = {"flag": False}

        def flaky_commit():
            if not healing_commit_started["flag"]:
                import inspect
                frame = inspect.currentframe()
                caller_locals = frame.f_back.f_locals if frame.f_back else {}
                step_obj = caller_locals.get("step_obj")
                if step_obj is not None and getattr(step_obj, "healed_selector", None):
                    healing_commit_started["flag"] = True
                    raise SQLAlchemyError("db lost on heal persist")
            return original_commit()

        with _DefaultPatches() as patches:
            _wire_crud(patches.crud, db, case_id, sample_project["id"])
            patches.executor.side_effect = [
                _executor_result(False, 1, error="element not found"),
                _executor_result(True, 1, action="click(#healed)"),
            ]
            with patch(
                "core.self_healing.try_heal_and_retry",
                new=AsyncMock(return_value="#healed"),
            ):
                db.commit = flaky_commit
                try:
                    result = await run_test_case_in_browser(
                        case_id, mcp, db=db, batch_id=batch_id,
                    )
                finally:
                    db.commit = original_commit

        assert result["status"] == "passed"
        assert healing_commit_started["flag"] is True


# ---------------------------------------------------------------------------
# Debug mode decisions
# ---------------------------------------------------------------------------


class TestRunTestCaseInBrowserDebugMode:
    @pytest.mark.asyncio
    async def test_debug_abort_invokes_abort_branch(
        self, db, sample_project
    ):
        case_id = _create_case_with_steps(
            db, sample_project["id"], "debug-abort", 3,
        )
        mcp = _wire_mcp()
        batch_id = _create_batch(db, sample_project["id"])

        with _DefaultPatches() as patches:
            _wire_crud(patches.crud, db, case_id, sample_project["id"])
            patches.executor.side_effect = [
                _executor_result(False, 1, error="boom"),
            ]

            with patch("core.runner._execution.LogBroadcaster") as MockBC, \
                 patch("core.runner._execution._pause_decisions",
                       _DecisionDict("abort")):
                mock_pause_event = MagicMock()
                mock_pause_event.wait = AsyncMock()
                MockBC.get_pause_event = MagicMock(return_value=mock_pause_event)
                MockBC.log_step_start = AsyncMock()
                MockBC.log_step_complete = AsyncMock()
                MockBC.log_execution_paused = AsyncMock()
                MockBC.log_info = AsyncMock()

                result = await run_test_case_in_browser(
                    case_id, mcp, db=db, batch_id=batch_id, debug_mode=True,
                )

        assert result["status"] == "passed"
        assert patches.executor.await_count == 1

    @pytest.mark.asyncio
    async def test_debug_edit_retries_with_new_description(
        self, db, sample_project
    ):
        case_id = _create_case_with_steps(
            db, sample_project["id"], "debug-edit", 1,
        )
        mcp = _wire_mcp()
        batch_id = _create_batch(db, sample_project["id"])

        with _DefaultPatches() as patches:
            _wire_crud(patches.crud, db, case_id, sample_project["id"])
            patches.executor.side_effect = [
                _executor_result(False, 1, error="boom"),
                _executor_result(True, 1, action="click(#new)"),
            ]

            decision_dict = _DecisionDict(
                "edit", new_description="edit 后描述",
            )
            with patch("core.runner._execution.LogBroadcaster") as MockBC, \
                 patch("core.runner._execution._pause_decisions", decision_dict):
                mock_pause_event = MagicMock()
                mock_pause_event.wait = AsyncMock()
                MockBC.get_pause_event = MagicMock(return_value=mock_pause_event)
                MockBC.log_step_start = AsyncMock()
                MockBC.log_step_complete = AsyncMock()
                MockBC.log_execution_paused = AsyncMock()
                MockBC.log_info = AsyncMock()

                result = await run_test_case_in_browser(
                    case_id, mcp, db=db, batch_id=batch_id, debug_mode=True,
                )

        assert result["status"] == "passed"
        from app import db_models
        step_row = db.query(db_models.TestStep).filter(
            db_models.TestStep.case_id == case_id
        ).first()
        assert step_row.description == "edit 后描述"

    @pytest.mark.asyncio
    async def test_debug_retry_recovers_step(
        self, db, sample_project
    ):
        case_id = _create_case_with_steps(
            db, sample_project["id"], "debug-retry", 1,
        )
        mcp = _wire_mcp()
        batch_id = _create_batch(db, sample_project["id"])

        with _DefaultPatches() as patches:
            _wire_crud(patches.crud, db, case_id, sample_project["id"])
            patches.executor.side_effect = [
                _executor_result(False, 1, error="first fail"),
                _executor_result(True, 1, action="click(after-retry)"),
            ]

            with patch("core.runner._execution.LogBroadcaster") as MockBC, \
                 patch("core.runner._execution._pause_decisions",
                       _DecisionDict("retry")):
                mock_pause_event = MagicMock()
                mock_pause_event.wait = AsyncMock()
                MockBC.get_pause_event = MagicMock(return_value=mock_pause_event)
                MockBC.log_step_start = AsyncMock()
                MockBC.log_step_complete = AsyncMock()
                MockBC.log_execution_paused = AsyncMock()
                MockBC.log_info = AsyncMock()

                result = await run_test_case_in_browser(
                    case_id, mcp, db=db, batch_id=batch_id, debug_mode=True,
                )

        assert result["status"] == "passed"

    @pytest.mark.asyncio
    async def test_debug_skip_keeps_remaining_steps(
        self, db, sample_project
    ):
        case_id = _create_case_with_steps(
            db, sample_project["id"], "debug-skip", 2,
        )
        mcp = _wire_mcp()
        batch_id = _create_batch(db, sample_project["id"])

        with _DefaultPatches() as patches:
            _wire_crud(patches.crud, db, case_id, sample_project["id"])
            patches.executor.side_effect = [
                _executor_result(False, 1, error="boom"),
            ]

            with patch("core.runner._execution.LogBroadcaster") as MockBC, \
                 patch("core.runner._execution._pause_decisions",
                       _DecisionDict("skip")):
                mock_pause_event = MagicMock()
                mock_pause_event.wait = AsyncMock()
                MockBC.get_pause_event = MagicMock(return_value=mock_pause_event)
                MockBC.log_step_start = AsyncMock()
                MockBC.log_step_complete = AsyncMock()
                MockBC.log_execution_paused = AsyncMock()
                MockBC.log_info = AsyncMock()

                result = await run_test_case_in_browser(
                    case_id, mcp, db=db, batch_id=batch_id, debug_mode=True,
                )
        assert len(result["step_results"]) == 2


# ---------------------------------------------------------------------------
# Per-step assertions
# ---------------------------------------------------------------------------


class TestRunTestCaseInBrowserAssertions:
    @pytest.mark.asyncio
    async def test_assertion_pass_keeps_step_success(
        self, db, sample_project
    ):
        case_id = _create_case_with_steps(
            db, sample_project["id"], "assert-ok", 1,
            assertions=[{"type": "text", "expected": "登录成功"}],
        )
        mcp = _wire_mcp()
        batch_id = _create_batch(db, sample_project["id"])

        with _DefaultPatches() as patches:
            _wire_crud(patches.crud, db, case_id, sample_project["id"])
            with patch(
                "core.runner._execution.execute_step_assertions",
                new=AsyncMock(return_value=[{"passed": True, "type": "text"}]),
            ) as mock_assert:
                result = await run_test_case_in_browser(
                    case_id, mcp, db=db, batch_id=batch_id,
                )
        assert result["status"] == "passed"
        mock_assert.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_assertion_fail_skips_remaining_steps(
        self, db, sample_project
    ):
        case_id = _create_case_with_steps(
            db, sample_project["id"], "assert-fail", 2,
            assertions=[{"type": "text", "expected": "missing"}],
        )
        mcp = _wire_mcp()
        batch_id = _create_batch(db, sample_project["id"])

        with _DefaultPatches() as patches:
            _wire_crud(patches.crud, db, case_id, sample_project["id"])
            with patch(
                "core.runner._execution.execute_step_assertions",
                new=AsyncMock(return_value=[{"passed": False, "type": "text"}]),
            ):
                result = await run_test_case_in_browser(
                    case_id, mcp, db=db, batch_id=batch_id,
                )

        assert result["status"] == "failed"
        assert any(r["status"] == "skipped" for r in result["step_results"])

    @pytest.mark.asyncio
    async def test_assertion_exception_does_not_break_run(
        self, db, sample_project
    ):
        case_id = _create_case_with_steps(
            db, sample_project["id"], "assert-exc", 1,
            assertions=[{"type": "text", "expected": "x"}],
        )
        mcp = _wire_mcp()
        batch_id = _create_batch(db, sample_project["id"])

        with _DefaultPatches() as patches:
            _wire_crud(patches.crud, db, case_id, sample_project["id"])
            with patch(
                "core.runner._execution.execute_step_assertions",
                new=AsyncMock(side_effect=RuntimeError("assertion engine died")),
            ):
                result = await run_test_case_in_browser(
                    case_id, mcp, db=db, batch_id=batch_id,
                )
        assert result["status"] == "passed"

    @pytest.mark.asyncio
    async def test_assertion_fail_debug_abort(
        self, db, sample_project
    ):
        case_id = _create_case_with_steps(
            db, sample_project["id"], "assert-fail-debug", 2,
            assertions=[{"type": "text", "expected": "x"}],
        )
        mcp = _wire_mcp()
        batch_id = _create_batch(db, sample_project["id"])

        with _DefaultPatches() as patches:
            _wire_crud(patches.crud, db, case_id, sample_project["id"])
            with patch(
                "core.runner._execution.execute_step_assertions",
                new=AsyncMock(return_value=[{"passed": False, "type": "text"}]),
            ):
                with patch("core.runner._execution.LogBroadcaster") as MockBC, \
                     patch("core.runner._execution._pause_decisions",
                           _DecisionDict("abort")):
                    mock_pause_event = MagicMock()
                    mock_pause_event.wait = AsyncMock()
                    MockBC.get_pause_event = MagicMock(return_value=mock_pause_event)
                    MockBC.log_step_start = AsyncMock()
                    MockBC.log_step_complete = AsyncMock()
                    MockBC.log_execution_paused = AsyncMock()
                    MockBC.log_info = AsyncMock()

                    result = await run_test_case_in_browser(
                        case_id, mcp, db=db, batch_id=batch_id, debug_mode=True,
                    )
        assert result["status"] == "passed"

    @pytest.mark.asyncio
    async def test_assertion_fail_debug_skip_continues(
        self, db, sample_project
    ):
        case_id = _create_case_with_steps(
            db, sample_project["id"], "assert-fail-skip", 2,
            assertions=[{"type": "text", "expected": "x"}],
        )
        mcp = _wire_mcp()
        batch_id = _create_batch(db, sample_project["id"])

        with _DefaultPatches() as patches:
            _wire_crud(patches.crud, db, case_id, sample_project["id"])
            with patch(
                "core.runner._execution.execute_step_assertions",
                new=AsyncMock(return_value=[{"passed": False, "type": "text"}]),
            ):
                with patch("core.runner._execution.LogBroadcaster") as MockBC, \
                     patch("core.runner._execution._pause_decisions",
                           _DecisionDict("skip")):
                    mock_pause_event = MagicMock()
                    mock_pause_event.wait = AsyncMock()
                    MockBC.get_pause_event = MagicMock(return_value=mock_pause_event)
                    MockBC.log_step_start = AsyncMock()
                    MockBC.log_step_complete = AsyncMock()
                    MockBC.log_execution_paused = AsyncMock()
                    MockBC.log_info = AsyncMock()

                    result = await run_test_case_in_browser(
                        case_id, mcp, db=db, batch_id=batch_id, debug_mode=True,
                    )
        assert len(result["step_results"]) == 2


# ---------------------------------------------------------------------------
# Outer exception handler
# ---------------------------------------------------------------------------


class TestRunTestCaseInBrowserOuterErrors:
    @pytest.mark.asyncio
    async def test_outer_exception_marks_run_failed(
        self, db, sample_project
    ):
        case_id = _create_case_with_steps(
            db, sample_project["id"], "outer-err", 1,
        )
        mcp = _wire_mcp()
        batch_id = _create_batch(db, sample_project["id"])

        with _DefaultPatches() as patches:
            patches.crud.get_test_case.side_effect = RuntimeError("DB went down")
            result = await run_test_case_in_browser(
                case_id, mcp, db=db, batch_id=batch_id,
            )

        assert result["status"] == "failed"
        assert "step_results" in result


# ---------------------------------------------------------------------------
# No run_id path (save_run_results branch when no run created)
# ---------------------------------------------------------------------------


class TestRunTestCaseInBrowserNoRunId:
    @pytest.mark.asyncio
    async def test_save_run_results_called_when_run_creation_fails(
        self, db, sample_project
    ):
        case_id = _create_case_with_steps(
            db, sample_project["id"], "no-run-id", 1,
        )
        mcp = _wire_mcp()
        batch_id = _create_batch(db, sample_project["id"])

        with _DefaultPatches() as patches:
            _wire_crud(patches.crud, db, case_id, sample_project["id"])
            with patch("core.runner._execution.mark_run_running", return_value=False):
                with patch.object(db, "add", side_effect=SQLAlchemyError("db dead")):
                    result = await run_test_case_in_browser(
                        case_id, mcp, db=db, batch_id=batch_id, run_id=999999,
                    )
        assert result["status"] == "failed"


# ---------------------------------------------------------------------------
# Own DB session
# ---------------------------------------------------------------------------


class TestRunTestCaseInBrowserOwnDb:
    @pytest.mark.asyncio
    async def test_creates_and_closes_own_session(
        self, db, sample_project, monkeypatch
    ):
        case_id = _create_case_with_steps(
            db, sample_project["id"], "own-db", 1,
        )
        mcp = _wire_mcp()
        batch_id = _create_batch(db, sample_project["id"])

        with _DefaultPatches() as patches:
            _wire_crud(patches.crud, db, case_id, sample_project["id"])
            monkeypatch.setattr(
                "core.runner._execution.SessionLocal", lambda: db
            )
            result = await run_test_case_in_browser(
                case_id, mcp, batch_id=batch_id,
            )

        assert result["status"] == "passed"
