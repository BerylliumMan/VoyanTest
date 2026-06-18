# core/runner/_execution.py
"""单条测试用例在浏览器中执行的引擎（MCP 驱动）。

run_test_case_in_browser 是核心入口：在一个已启动的 PlaywrightMCPManager
上逐步骤执行一个测试用例，包含重试、自愈、断言和交互式调试。
"""

import asyncio
import json as _json
import logging
import os
import uuid
from typing import Optional

import sqlalchemy as sa
from sqlalchemy.exc import SQLAlchemyError

from app.tz import now as tz_now

from app import crud
from app.database import SessionLocal
from app.websocket import LogBroadcaster
from core.assertions import execute_assertions as execute_step_assertions
from core.llm_wrapper import create_openai_client, _resolve_config as _resolve_llm_config
from core.step_executor import execute_step_mcp

# 暂停 / 决策字典直接来自 app.websocket —— 不在 _state.py 中转，
# 避免 core.runner 包内出现间接依赖循环。
from app.websocket import _pause_events, _pause_decisions
from core.runner._state import _is_healable_error
from core.runner._validators import _validate_nav_url, _resolve_env_cookies, _inject_auth_cookies
from core.runner._persistence import (
    mark_run_running,
    save_run_results,
    update_run_on_completion,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Main test runner (MCP-based)
# ---------------------------------------------------------------------------


async def run_test_case_in_browser(
    case_id: int,
    mcp_manager,
    db=None,
    *,
    batch_id: int | None = None,
    clear_cookies: bool = False,
    run_id: int | None = None,
    base_url_override: str | None = None,
    debug_mode: bool = False,
) -> dict:
    """Execute a single test case using an existing PlaywrightMCPManager.

    Parameters
    ----------
    case_id : int
        The test case to execute.
    mcp_manager : PlaywrightMCPManager
        An already-started manager (shared across batch runs).
    db : Session, optional
        Database session.  Created internally when *None*.
    batch_id : int, optional
        RunBatch ID to associate this run with.
    clear_cookies : bool
        If True, clear browser cookies before execution (used once at
        batch-start, not per-case).
    debug_mode : bool
        If True, pauses execution on step failure for interactive
        debugging via WebSocket (retry / skip / abort / edit).

    Returns
    -------
    dict with keys: case_id, status, report_path, step_results, batch_id
    """
    own_db = db is None
    if own_db:
        db = SessionLocal()

    case_logger = logging.getLogger(f"runner.case_{case_id}")

    start_time = tz_now()
    run_log_entries: list[dict] = []
    test_status = "failed"
    case_report_path: Optional[str] = None
    file_handler = None
    from app import db_models

    try:
        case_data = crud.get_test_case(db, case_id)
        if not case_data:
            raise ValueError(f"Test case with ID {case_id} not found.")

        # 预创建 TestRun 记录 (pending) 或使用已预创建的
        now = tz_now()
        if run_id is not None:
            if not mark_run_running(db, run_id):
                logger.warning("TestRun %s not found, will create new record", run_id)
                run_id = None
            else:
                logger.info(f"TestRun {run_id} updated (pending -> running)")
        if run_id is None:
            pending_run = db_models.TestRun(
                case_id=case_id, batch_id=batch_id, status="pending",
                start_time=now, end_time=now,
            )
            db.add(pending_run)
            db.commit()
            db.refresh(pending_run)
            run_id = pending_run.id
            pending_run.status = "running"
            db.commit()
            logger.info(f"TestRun {run_id} created + started (running)")

        project_data = crud.get_project(db, case_data.project_id)
        if not project_data:
            raise ValueError(f"Project with ID {case_data.project_id} not found.")

        run_uid = uuid.uuid4().hex[:12]
        output_dir = os.path.join("reports", f"run_{case_id}_{run_uid}")
        os.makedirs(output_dir, exist_ok=True)

        log_file_path = os.path.join(output_dir, "run.log")
        file_handler = logging.FileHandler(log_file_path)
        file_handler.setFormatter(
            logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
        )
        case_logger = logging.getLogger(f"runner.case_{case_id}")
        case_logger.addHandler(file_handler)

        logger.info(f"Starting MCP execution: '{case_data.name}'")

        steps_raw = crud.get_steps_for_case(db, case_id)
        step_list = [
            {'id': s.id, 'step_order': s.step_order, 'description': s.description, 'expected_result': s.parsed_result}
            for s in steps_raw
        ]
        step_list.sort(key=lambda x: x['step_order'])

        if not step_list:
            raise ValueError("Test case has no steps")

        if clear_cookies:
            await mcp_manager.clear_cookies()

        # Resolve nav URL first so we can derive default cookie domain from it
        nav_url = _validate_nav_url(base_url_override or project_data.base_url)

        # Inject environment-defined auth cookies BEFORE navigation so the
        # browser already presents the authenticated session on the first page.
        env_cookies = _resolve_env_cookies(db, base_url_override)
        if env_cookies:
            await _inject_auth_cookies(mcp_manager, env_cookies, nav_url)

        if nav_url:
            nav_result = await mcp_manager.call_tool('browser_navigate', {'url': nav_url})
            if not nav_result.get('success'):
                logger.warning("Failed to navigate to %s: %s", nav_url, nav_result.get('text'))
            else:
                logger.info(f"Navigated to {nav_url}")

        llm_client = create_openai_client()
        _, _, resolved_model = _resolve_llm_config()

        # ------------------------------------------------------------------
        # Execute steps
        # ------------------------------------------------------------------
        step_results: list[dict] = []
        consecutive_failures = 0
        max_failures = 1

        screenshot_dir = os.path.join(output_dir, "screenshots")
        os.makedirs(screenshot_dir, exist_ok=True)

        should_abort = False
        for idx, (step_obj, step_dict) in enumerate(zip(steps_raw, step_list)):
            if should_abort:
                # 因 abort 决策跳过剩余步骤
                step_results.append({
                    'step_number': step_dict['step_order'],
                    'original_description': step_dict['description'],
                    'success': False,
                    'status': 'skipped',
                    'thinking': '',
                    'action': '',
                    'next_goal': '',
                    'error': '用户中止执行',
                    'screenshot_path': None,
                    'duration_ms': 0,
                })
                continue

            logger.info(
                f"--- Step {step_dict['step_order']}: {step_dict['description']} ---"
            )

            # 获取重试配置（兼容旧字段，默认无重试）
            retry_max = getattr(step_obj, 'retry_max', 0) or 0
            retry_delay = getattr(step_obj, 'retry_delay', 1.0) or 1.0

            step_success = False
            last_result = None
            step_start_time = tz_now()

            # ---- 广播步骤开始 ----
            await LogBroadcaster.log_step_start(
                run_id,
                step_id=step_obj.id,
                step_description=f"[步骤{step_obj.step_order}] {step_obj.description}",
            )

            # ==============================================================
            # 重试循环
            # ==============================================================
            for attempt in range(retry_max + 1):
                if attempt > 0:
                    logger.info(f"  重试第 {attempt}/{retry_max} 次...")
                    await asyncio.sleep(retry_delay)
                    # 清除上次暂停事件，确保全新等待
                    if run_id in _pause_events:
                        _pause_events[run_id] = asyncio.Event()

                try:
                    result = await execute_step_mcp(
                        step_dict,
                        mcp_manager,
                        llm_client,
                        model=resolved_model,
                        step_timeout_ms=120000,
                        screenshot_dir=screenshot_dir,
                    )
                except Exception as step_exc:
                    # Broad catch is necessary: execute_step_mcp touches MCP stdio,
                    # LLM HTTP, JSON parsing, and asyncio — any failure must be
                    # converted into a structured "step failed" result so the
                    # outer retry/self-heal/assertion pipeline can still proceed.
                    logger.exception(
                        "execute_step_mcp raised for step %s (attempt %d)",
                        step_dict['step_order'], attempt,
                    )
                    result = {
                        'step_number': step_dict['step_order'],
                        'original_description': step_dict['description'],
                        'success': False,
                        'status': 'error',
                        'thinking': '',
                        'action': '',
                        'next_goal': '',
                        'error': str(step_exc),
                        'screenshot_path': None,
                        'duration_ms': 0,
                    }

                step_duration = (tz_now() - step_start_time).total_seconds()

                if result['success']:
                    step_success = True
                    last_result = result
                    # 广播步骤成功
                    await LogBroadcaster.log_step_complete(
                        run_id, step_id=step_obj.id,
                        status="passed", duration=step_duration,
                    )
                    break  # 跳出重试循环

                # ---- 本次尝试失败 ----
                last_result = result
                if attempt < retry_max:
                    logger.warning(
                        f"  步骤 {step_dict['step_order']} 尝试 {attempt+1}/{retry_max+1} 失败"
                        f"{': ' + result.get('error', '') if result.get('error') else ''}"
                    )

                # ---- 自愈选择器：仅在首次失败且错误为定位类时尝试 ----
                error_msg = (result.get('error') or '').strip()
                if attempt == 0 and not step_success and _is_healable_error(error_msg):
                    from core.self_healing import try_heal_and_retry

                    healed = await try_heal_and_retry(
                        mcp_manager,
                        step_dict=step_dict,
                        step_obj=step_obj,
                        step_description=step_obj.description,
                        error=result.get('error', ''),
                    )

                    if healed:
                        logger.info(
                            f"  🔧 自愈选择器生效: {step_dict['description']} → {healed}"
                        )
                        # 更新步骤描述以使用修复后的选择器
                        step_dict['description'] = healed
                        # 持久化到数据库
                        if db is not None:
                            try:
                                step_obj.healed_selector = healed
                                db.commit()
                            except SQLAlchemyError as db_exc:
                                logger.warning("保存自愈选择器失败: %s", db_exc, exc_info=True)
                        continue  # 用新选择器重试
                    elif attempt == 0 and not step_success and error_msg:
                        logger.debug(f"  跳过自愈（非定位错误）: {error_msg[:80]}")

            # ==============================================================
            # 步骤最终失败处理
            # ==============================================================
            if not step_success:
                # 广播步骤失败
                step_duration = (tz_now() - step_start_time).total_seconds()
                await LogBroadcaster.log_step_complete(
                    run_id, step_id=step_obj.id,
                    status="failed", duration=step_duration,
                )

                # ---- 调试模式：暂停等待用户决策 ----
                if debug_mode:
                    await LogBroadcaster.log_execution_paused(
                        run_id,
                        step_id=step_obj.id,
                        step_description=step_obj.description,
                        reason=last_result.get('error', '未知错误') if last_result else '未知错误',
                    )
                    pause_ev = LogBroadcaster.get_pause_event(run_id)
                    await pause_ev.wait()

                    decision = _pause_decisions.get(run_id, {}).get("decision", "abort")

                    if decision == "retry":
                        logger.info(f"  用户选择重试步骤 {step_obj.step_order}")
                        # 重置失败计数，重新执行整个重试循环
                        step_success = False
                        for retry_attempt in range(retry_max + 1):
                            if retry_attempt > 0:
                                await asyncio.sleep(retry_delay)
                            try:
                                result = await execute_step_mcp(
                                    step_dict,
                                    mcp_manager,
                                    llm_client,
                                    model=resolved_model,
                                    step_timeout_ms=120000,
                                    screenshot_dir=screenshot_dir,
                                )
                            except Exception as step_exc:
                                # Broad catch is necessary: execute_step_mcp touches MCP stdio,
                                # LLM HTTP, JSON parsing, and asyncio — any failure must be
                                # converted into a structured "step failed" result so the
                                # outer retry/self-heal/assertion pipeline can still proceed.
                                logger.exception(
                                    "execute_step_mcp raised during user-retry for step %s (attempt %d)",
                                    step_dict['step_order'], retry_attempt,
                                )
                                result = {
                                    'step_number': step_dict['step_order'],
                                    'original_description': step_dict['description'],
                                    'success': False,
                                    'status': 'error',
                                    'thinking': '',
                                    'action': '',
                                    'next_goal': '',
                                    'error': str(step_exc),
                                    'screenshot_path': None,
                                    'duration_ms': 0,
                                }
                            if result['success']:
                                step_success = True
                                last_result = result
                                break
                        # 如果 retry 后成功，继续正常流程
                        if step_success:
                            await LogBroadcaster.log_step_complete(
                                run_id, step_id=step_obj.id,
                                status="passed",
                                duration=(tz_now() - step_start_time).total_seconds(),
                            )
                    elif decision == "skip":
                        logger.info(f"  用户选择跳过步骤 {step_obj.step_order}")
                        # 将失败结果标记为 skipped 再继续
                        if last_result:
                            last_result['status'] = 'skipped'
                            last_result['error'] = (last_result.get('error') or '') + ' (用户跳过)'
                    elif decision == "edit":
                        new_desc = _pause_decisions.get(run_id, {}).get("new_description", "")
                        if new_desc:
                            logger.info(f"  用户编辑步骤描述: {new_desc}")
                            # 更新 DB 和本地 step_dict
                            step_obj.description = new_desc
                            step_dict['description'] = new_desc
                            try:
                                db.commit()
                            except SQLAlchemyError:
                                logger.exception("保存用户编辑后的步骤描述失败")
                                db.rollback()
                            # 使用新描述重试
                            for retry_attempt in range(retry_max + 1):
                                if retry_attempt > 0:
                                    await asyncio.sleep(retry_delay)
                                try:
                                    result = await execute_step_mcp(
                                        step_dict,
                                        mcp_manager,
                                        llm_client,
                                        model=resolved_model,
                                        step_timeout_ms=120000,
                                        screenshot_dir=screenshot_dir,
                                    )
                                except Exception as step_exc:
                                    # Broad catch is necessary: execute_step_mcp touches MCP stdio,
                                    # LLM HTTP, JSON parsing, and asyncio — any failure must be
                                    # converted into a structured "step failed" result so the
                                    # outer retry/self-heal/assertion pipeline can still proceed.
                                    logger.exception(
                                        "execute_step_mcp raised during user-edit-retry for step %s (attempt %d)",
                                        step_dict['step_order'], retry_attempt,
                                    )
                                    result = {
                                        'step_number': step_dict['step_order'],
                                        'original_description': step_dict['description'],
                                        'success': False,
                                        'status': 'error',
                                        'thinking': '',
                                        'action': '',
                                        'next_goal': '',
                                        'error': str(step_exc),
                                        'screenshot_path': None,
                                        'duration_ms': 0,
                                    }
                                if result['success']:
                                    step_success = True
                                    last_result = result
                                    break
                            if step_success:
                                await LogBroadcaster.log_step_complete(
                                    run_id, step_id=step_obj.id,
                                    status="passed",
                                    duration=(tz_now() - step_start_time).total_seconds(),
                                )
                    else:  # "abort" 或其他
                        logger.info(f"  用户选择中止执行")
                        should_abort = True
                        # 重置暂停事件，避免后续误用
                        if run_id in _pause_events:
                            _pause_events[run_id] = asyncio.Event()

                    # 清除本次决策，避免污染下次
                    _pause_decisions.pop(run_id, None)

                # 如果 abort 被触发，跳出主循环
                if should_abort:
                    break

                # 记录失败（非 skip / 非 retry 成功的情况）
                if not step_success:
                    consecutive_failures += 1
                else:
                    consecutive_failures = 0

            if not step_success and last_result is not None:
                run_log_entries.append({
                    'step_id': step_dict['id'],
                    'level': 'ERROR' if not step_success else 'INFO',
                    'message': (
                        last_result.get('error')
                        or f"Completed: {last_result.get('action', '')}"
                    ),
                    'screenshot_path': last_result.get('screenshot_path'),
                })
                step_results.append(last_result)
            elif last_result is not None:
                # 步骤成功
                run_log_entries.append({
                    'step_id': step_dict['id'],
                    'level': 'INFO',
                    'message': f"Completed: {last_result.get('action', '')}",
                    'screenshot_path': last_result.get('screenshot_path'),
                })
                step_results.append(last_result)

            # ==============================================================
            # 步骤成功后的断言验证
            # ==============================================================
            if step_success:
                step_assertions = getattr(step_obj, 'assertions', None)
                if not step_assertions:
                    step_assertions = []
                if not isinstance(step_assertions, list):
                    step_assertions = []

                if step_assertions:
                    try:
                        assertion_results = await execute_step_assertions(
                            mcp_manager, step_assertions
                        )
                        all_passed = all(
                            r.get('passed', False) for r in assertion_results
                        )
                        for r in assertion_results:
                            await LogBroadcaster.log_info(
                                run_id,
                                f"[断言] {r.get('type', '?')}: {'通过' if r.get('passed') else '失败'}",
                                step_id=step_obj.id,
                            )
                        if not all_passed:
                            logger.warning(
                                f"  步骤 {step_dict['step_order']} 断言失败"
                            )
                            # 断言失败 → 也视为步骤失败用于连续失败计数
                            if debug_mode:
                                await LogBroadcaster.log_execution_paused(
                                    run_id,
                                    step_id=step_obj.id,
                                    step_description=step_obj.description,
                                    reason="断言验证失败",
                                )
                                pause_ev = LogBroadcaster.get_pause_event(run_id)
                                await pause_ev.wait()
                                decision = _pause_decisions.get(run_id, {}).get("decision", "abort")
                                _pause_decisions.pop(run_id, None)
                                if decision == "abort":
                                    should_abort = True
                                    if run_id in _pause_events:
                                        _pause_events[run_id] = asyncio.Event()
                                    break
                                # skip / retry / edit 对断言失败也适用
                                # 简化处理：skip → 继续，其他 → 继续（断言失败不致命）
                            consecutive_failures += 1
                    except Exception as assert_exc:
                        # Broad catch is necessary: execute_step_assertions drives
                        # MCP tool calls + LLM calls + DOM inspection — any failure
                        # must be recorded as a non-fatal warning so the rest of
                        # the run can continue (assertions are advisory only).
                        logger.exception(
                            "步骤 %s 断言执行异常",
                            step_dict['step_order'],
                        )
                else:
                    consecutive_failures = 0
            else:
                consecutive_failures = min(consecutive_failures, max_failures)

            # ---- 连续失败达到上限 ----
            if consecutive_failures >= max_failures:
                failed_step_number = step_dict['step_order']
                logger.warning(
                    f"步骤 {failed_step_number} 失败，跳过后续步骤"
                )
                for remaining_obj, remaining_dict in zip(
                    steps_raw[idx + 1:], step_list[idx + 1:]
                ):
                    step_results.append({
                        'step_number': remaining_dict['step_order'],
                        'original_description': remaining_dict['description'],
                        'success': False,
                        'status': 'skipped',
                        'thinking': '',
                        'action': '',
                        'next_goal': '',
                        'error': f'因步骤{failed_step_number}失败而跳过',
                        'screenshot_path': None,
                        'duration_ms': 0,
                    })
                break

        # ------------------------------------------------------------------
        # Report
        # ------------------------------------------------------------------
        all_passed = all(r['success'] for r in step_results)
        test_status = "passed" if all_passed else "failed"

        report = {
            "test_case_id": case_id,
            "test_case_name": case_data.name,
            "status": test_status,
            "start_time": start_time.isoformat(),
            "end_time": tz_now().isoformat(),
            "duration": (tz_now() - start_time).total_seconds(),
            "steps": step_results,
        }

        case_report_path = os.path.join(output_dir, "report.json")
        with open(case_report_path, "w", encoding="utf-8") as f:
            _json.dump(report, f, ensure_ascii=False, indent=2)
        logger.info(f"Report saved: {case_report_path}")

    except Exception as exc:
        # Broad catch is necessary: this is the outer guard for the whole
        # run_test_case_in_browser flow (DB, MCP, LLM, filesystem, assertions).
        # Any unhandled error must be recorded as CRITICAL and propagate as a
        # "failed" status so the finally block can still persist results.
        test_status = "failed"
        logger.exception("Error executing test case %s", case_id)
        run_log_entries.append({
            "step_id": None,
            "level": "CRITICAL",
            "message": str(exc),
            "screenshot_path": None,
        })

    finally:
        end_time = tz_now()
        duration = (end_time - start_time).total_seconds()

        if run_id is not None:
            update_run_on_completion(
                db, run_id, test_status, start_time, end_time,
                duration, case_report_path, run_log_entries,
                batch_id=batch_id,
            )
            logger.info(f"TestRun {run_id} updated -> {test_status}")
        else:
            save_run_results(
                case_id, test_status, start_time, end_time,
                duration, case_report_path, None, run_log_entries,
                batch_id=batch_id,
            )

        if file_handler:
            case_logger.removeHandler(file_handler)
            file_handler.close()

        if own_db:
            db.close()

    return {
        "case_id": case_id,
        "status": test_status,
        "report_path": case_report_path,
        "step_results": step_results if 'step_results' in locals() else [],
        "batch_id": batch_id,
    }
