# core/runner/_orchestrator.py
"""用例编排层 — 创建浏览器/MCP 实例，调度单用例或批量执行。"""
import logging

from app.tz import now as tz_now

from app import crud
from app.database import SessionLocal

from core.runner._execution import run_test_case_in_browser
from core.runner._persistence import save_run_results

logger = logging.getLogger(__name__)


async def run_test_case(case_id: int, batch_id: int | None = None, environment_id: int | None = None, debug_mode: bool = False):
    """Execute a test case using LLM + Playwright MCP (npx subprocess).

    Backward-compatible wrapper that creates its own PlaywrightMCPManager.
    """
    from core.playwright_manager import PlaywrightMCPManager

    browser_type = 'chromium'
    headless = True
    base_url_override = None
    if environment_id:
        _db = SessionLocal()
        try:
            env = crud.get_environment(_db, environment_id)
            if env:
                browser_type = env.browser
                headless = env.headless
                base_url_override = env.base_url
        except Exception as exc:
            logger.warning(f"Environment lookup failed for env_id={environment_id}: {exc}")
        finally:
            _db.close()

    mcp_manager = PlaywrightMCPManager(browser_type=browser_type, headless=headless)
    start_time = tz_now()
    try:
        await mcp_manager.start()
    except Exception as exc:
        logger.error(f"Failed to start MCP manager for case {case_id}: {exc}", exc_info=True)
        save_run_results(
            case_id, "failed", start_time, tz_now(),
            (tz_now() - start_time).total_seconds(),
            None, None,
            [{
                "step_id": None,
                "level": "CRITICAL",
                "message": f"Browser startup failed: {exc}",
                "screenshot_path": None,
            }],
            batch_id=batch_id,
        )
        try:
            await mcp_manager.stop()
        except Exception as stop_exc:
            logger.info(f"MCP stop after start failure: {stop_exc}")
        return {"case_id": case_id, "status": "failed", "error": str(exc)}

    try:
        await run_test_case_in_browser(case_id, mcp_manager, batch_id=batch_id, base_url_override=base_url_override, debug_mode=debug_mode)
    except Exception as exc:
        logger.error(f"Unhandled error in run_test_case_in_browser for case {case_id}: {exc}", exc_info=True)
    finally:
        try:
            await mcp_manager.stop()
        except Exception:
            logger.warning("Failed to stop MCP manager")


async def run_batch_test_cases(
    case_ids: list[int],
    project_id: int,
    *,
    browser_pool=None,
    batch_id: int | None = None,
    environment_id: int | None = None,
    init_case_ids: list[int] | None = None,
    debug_mode: bool = False,
):
    """Execute multiple test cases sequentially in a single browser.

    Parameters
    ----------
    case_ids : list[int]
        IDs of test cases to execute.
    project_id : int
        Project owning the cases (used for browser pool lookup).
    browser_pool : BrowserPool class, optional
        Defaults to ``core.browser_pool.BrowserPool``.
    batch_id : int, optional
        RunBatch ID. If None, a new batch is created automatically.
    environment_id : int, optional
        Environment ID to use for browser settings and base URL.
    init_case_ids : list[int], optional
        IDs of initialization cases to run first.

    Each case executes in order; failures are caught per-case and logged,
    and the loop continues.  Browser cleanup happens in a finally block.
    """
    if browser_pool is None:
        from core.browser_pool import BrowserPool as browser_pool

    from core.playwright_manager import PlaywrightMCPManager

    total_main = len(case_ids)
    total_init = len(init_case_ids) if init_case_ids else 0
    total_cases = total_main + total_init

    # 创建批次（如果未提供 batch_id）
    if batch_id is None:
        _db = SessionLocal()
        try:
            batch = crud.create_run_batch(_db, project_id, total_cases=total_cases)
            batch_id = batch.id
        finally:
            _db.close()

    mcp_manager = None

    base_url_override = None
    try:
        # Get project/environment browser settings
        _db = SessionLocal()
        try:
            if environment_id:
                env = crud.get_environment(_db, environment_id)
                if env:
                    browser_type = env.browser
                    headless = env.headless
                    base_url_override = env.base_url
                else:
                    project_data = crud.get_project(_db, project_id)
                    browser_type = project_data.browser if project_data and project_data.browser else 'chromium'
                    headless = project_data.headless if project_data and project_data.headless is not None else True
            else:
                project_data = crud.get_project(_db, project_id)
                browser_type = project_data.browser if project_data and project_data.browser else 'chromium'
                headless = project_data.headless if project_data and project_data.headless is not None else True
        except Exception:
            browser_type = 'chromium'
            headless = True
        finally:
            _db.close()

        # 先预创建所有 pending TestRun 记录（在浏览器启动之前），
        # 确保即使浏览器启动失败，报告页面也能看到用例执行记录
        from app import db_models
        batch_db = SessionLocal()
        precreated_run_ids: dict[int, int] = {}
        try:
            for cid in (init_case_ids or []):
                pending_run = db_models.TestRun(
                    case_id=cid, batch_id=batch_id, status="pending",
                    start_time=None, end_time=None, is_init=True,
                )
                batch_db.add(pending_run)
                batch_db.flush()
                precreated_run_ids[cid] = pending_run.id
            for cid in case_ids:
                # start_time / end_time 留空：用 None 标记"尚未开始/结束"，
                # 避免被 crud._compute_batch_status 的"卡死 pending 30s"检查误判
                pending_run = db_models.TestRun(
                    case_id=cid, batch_id=batch_id, status="pending",
                    start_time=None, end_time=None,
                )
                batch_db.add(pending_run)
                batch_db.flush()
                precreated_run_ids[cid] = pending_run.id
            batch_db.commit()
        except Exception as exc:
            batch_db.rollback()
            logger.error(f"Failed to pre-create TestRun records: {exc}")

        # 创建或复用浏览器
        try:
            async def _factory():
                mgr = PlaywrightMCPManager(browser_type=browser_type, headless=headless)
                await mgr.start()
                return mgr

            existing = browser_pool.get_or_create(project_id, _factory)
            if existing is not None:
                mcp_manager = existing
            else:
                mcp_manager = await _factory()
                browser_pool.register(project_id, mcp_manager)
        except Exception as exc:
            logger.error(f"Failed to start browser for batch {batch_id}: {exc}")
            # 浏览器启动失败，将所有 pending 记录标记为 failed
            import sqlalchemy as sa
            _now = tz_now()
            for cid in ((init_case_ids or []) + case_ids):
                _rid = precreated_run_ids.get(cid)
                if _rid:
                    try:
                        _stmt = (
                            sa.update(db_models.TestRun)
                            .where(db_models.TestRun.id == _rid)
                            .values(
                                status="failed",
                                start_time=_now,
                                end_time=_now,
                                duration=0.0,
                            )
                        )
                        batch_db.execute(_stmt)
                        _log = db_models.RunLog(
                            run_id=_rid, step_id=None, level="CRITICAL",
                            message=f"Browser startup failed: {exc}",
                            screenshot_path=None,
                        )
                        batch_db.add(_log)
                    except Exception as exc:
                        logger.warning(f"Failed to save failure log for TestRun {_rid}: {exc}")
            try:
                batch_db.commit()
                crud.update_batch_counters(batch_db, batch_id, "failed")
                batch_db.commit()
            except Exception:
                batch_db.rollback()
            return

        # Clear cookies once at batch start
        await mcp_manager.clear_cookies()

        results = []
        # 先运行初始化用例，再运行主用例
        for case_id in (init_case_ids or []):
            _rid = precreated_run_ids.get(case_id)
            try:
                result = await run_test_case_in_browser(
                    case_id, mcp_manager, db=batch_db, clear_cookies=False,
                    batch_id=batch_id, run_id=_rid,
                    base_url_override=base_url_override,
                    debug_mode=debug_mode,
                )
                results.append(result)
                logger.info(f"Batch init-case {case_id} finished: {result['status']}")
            except Exception as exc:
                logger.error(f"Batch init-case {case_id} failed: {exc}", exc_info=True)
                _run_id = precreated_run_ids.get(case_id)
                if _run_id:
                    try:
                        import sqlalchemy as sa
                        _now = tz_now()
                        _stmt = sa.update(db_models.TestRun).where(db_models.TestRun.id == _run_id).values(status="failed", start_time=_now, end_time=_now, duration=0.0)
                        batch_db.execute(_stmt)
                        _log = db_models.RunLog(run_id=_run_id, step_id=None, level="CRITICAL", message=f"Batch init-case executor exception: {exc}", screenshot_path=None)
                        batch_db.add(_log)
                        batch_db.commit()
                        try: crud.update_batch_counters(batch_db, batch_id, "failed"); batch_db.commit()
                        except Exception: batch_db.rollback()
                    except Exception: batch_db.rollback()
                results.append({"case_id": case_id, "status": "failed", "error": str(exc), "batch_id": batch_id})

        # 运行主用例
        for case_id in case_ids:
            _rid = precreated_run_ids.get(case_id)
            try:
                result = await run_test_case_in_browser(
                    case_id, mcp_manager, db=batch_db, clear_cookies=False,
                    batch_id=batch_id, run_id=_rid,
                    base_url_override=base_url_override,
                    debug_mode=debug_mode,
                )
                results.append(result)
                logger.info(
                    f"Batch: case {case_id} finished: {result['status']}"
                )
            except Exception as exc:
                logger.error(
                    f"Batch: case {case_id} failed with exception: {exc}",
                    exc_info=True,
                )
                # 用 sqlalchemy update 直接更新预创建的 pending 记录为 failed
                _run_id = precreated_run_ids.get(case_id)
                if _run_id:
                    try:
                        import sqlalchemy as sa
                        _now = tz_now()
                        _stmt = (
                            sa.update(db_models.TestRun)
                            .where(db_models.TestRun.id == _run_id)
                            .values(
                                status="failed",
                                start_time=_now,
                                end_time=_now,
                                duration=0.0,
                            )
                        )
                        batch_db.execute(_stmt)
                        _log = db_models.RunLog(
                            run_id=_run_id, step_id=None, level="CRITICAL",
                            message=f"Batch executor exception: {exc}",
                            screenshot_path=None,
                        )
                        batch_db.add(_log)
                        batch_db.commit()
                        try:
                            crud.update_batch_counters(batch_db, batch_id, "failed")
                            batch_db.commit()
                        except Exception:
                            batch_db.rollback()
                    except Exception:
                        batch_db.rollback()
                results.append({
                    "case_id": case_id,
                    "status": "failed",
                    "error": str(exc),
                    "batch_id": batch_id,
                })

        return results

    finally:
        if 'batch_db' in locals():
            try:
                batch_db.close()
            except Exception as exc:
                logger.debug(f"Error closing batch_db: {exc}")
