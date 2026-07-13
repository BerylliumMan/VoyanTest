# core/runner/_orchestrator.py
"""用例编排层 — 创建浏览器/MCP 实例，调度单用例或批量执行。

职责：
    1. 单用例执行入口（run_test_case）— 创建 / 销毁浏览器
    2. 批量执行入口（run_batch_test_cases）— 共享浏览器 + 预创建
       pending TestRun + 统一批次跟踪

DB 操作的 SQL 细节已经下沉到 core.runner._persistence（mark_run_failed
/ precreate_pending_runs / update_run_on_completion），本模块只负责
协调浏览器/用例循环 + 批量跟踪。
"""
import logging

from sqlalchemy.exc import SQLAlchemyError

from app.tz import now as tz_now

from app import crud
from app.database import AsyncSessionLocal

from core.runner._execution import run_test_case_in_browser
from core.runner._persistence import (
    mark_run_failed,
    precreate_pending_runs,
    save_run_results,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 批量跟踪 helper（只在 _orchestrator 内复用，不下沉到 _persistence，
# 因为它依赖 precreated_run_ids 这一批处理特有的状态）
# ---------------------------------------------------------------------------


async def _record_batch_case_failure(
    db,
    precreated_run_ids: dict[int, int],
    case_id: int,
    batch_id: int,
    message: str,
) -> None:
    """批量执行中单条用例抛异常时，把预创建的 TestRun 标记为 failed。

    precreated_run_ids 是 run_batch_test_cases 内维护的
    ``{case_id: run_id}`` 映射；本函数仅在 key 命中时落库。
    """
    _run_id = precreated_run_ids.get(case_id)
    if _run_id:
        await mark_run_failed(db, _run_id, message, batch_id=batch_id)


# ---------------------------------------------------------------------------
# 单用例入口
# ---------------------------------------------------------------------------


async def run_test_case(case_id: int, batch_id: int | None = None, environment_id: int | None = None, debug_mode: bool = False):
    """Execute a test case using LLM + Playwright MCP (npx subprocess).

    Backward-compatible wrapper that creates its own PlaywrightMCPManager.
    """
    from core.playwright_manager import PlaywrightMCPManager

    browser_type = 'chromium'
    headless = True
    base_url_override = None
    if environment_id:
        async with AsyncSessionLocal() as _db:
            try:
                env = await crud.get_environment(_db, environment_id)
                if env:
                    browser_type = env.browser
                    headless = True  # 服务端始终用 headless 模式
                    base_url_override = env.base_url
            except SQLAlchemyError as exc:
                logger.warning("Environment lookup failed for env_id=%s: %s", environment_id, exc, exc_info=True)

    mcp_manager = PlaywrightMCPManager(browser_type=browser_type, headless=headless)
    start_time = tz_now()
    try:
        await mcp_manager.start()
    except Exception as exc:  # noqa: BLE001 - 见下方注释
        # Broad catch is necessary: PlaywrightMCPManager.start spawns an npx
        # subprocess, opens stdio pipes, and talks to a Playwright MCP server.
        # Failures can surface as OSError (subprocess), ConnectionError, or
        # asyncio.TimeoutError — any of them must be reported as a clean
        # "browser startup failed" so the run results stay consistent.
        logger.exception("Failed to start MCP manager for case %s", case_id)
        await save_run_results(
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
        except (OSError, RuntimeError) as stop_exc:
            logger.info("MCP stop after start failure: %s", stop_exc, exc_info=True)
        return {"case_id": case_id, "status": "failed", "error": str(exc)}

    try:
        await run_test_case_in_browser(case_id, mcp_manager, batch_id=batch_id, base_url_override=base_url_override, debug_mode=debug_mode)
    except Exception:  # noqa: BLE001 - 见下方注释
        # Broad catch is necessary: run_test_case_in_browser touches DB, MCP
        # stdio, LLM HTTP, JSON parsing, and assertions. Any unhandled error
        # must be logged so the batch can still proceed to cleanup.
        logger.exception("Unhandled error in run_test_case_in_browser for case %s", case_id)
    finally:
        try:
            await mcp_manager.stop()
        except (OSError, RuntimeError):
            logger.warning("Failed to stop MCP manager", exc_info=True)


# ---------------------------------------------------------------------------
# 批量执行入口
# ---------------------------------------------------------------------------


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

    Note: 该函数通过 FastAPI BackgroundTasks.add_task 调用。FastAPI 的
    BackgroundTasks 原生支持 async 协程，无需额外包装。
    """
    if browser_pool is None:
        from core.browser_pool import BrowserPool as browser_pool

    from core.playwright_manager import PlaywrightMCPManager

    total_main = len(case_ids)
    total_init = len(init_case_ids) if init_case_ids else 0
    total_cases = total_main + total_init

    # 创建批次（如果未提供 batch_id）
    if batch_id is None:
        async with AsyncSessionLocal() as _db:
            batch = await crud.create_run_batch(_db, project_id, total_cases=total_cases)
            batch_id = batch.id

    mcp_manager = None
    base_url_override = None

    async with AsyncSessionLocal() as batch_db:
        # Get project/environment browser settings
        try:
            if environment_id:
                env = await crud.get_environment(batch_db, environment_id)
                if env:
                    browser_type = env.browser
                    headless = True  # 服务端始终用 headless 模式
                    base_url_override = env.base_url
                else:
                    project_data = await crud.get_project(batch_db, project_id)
                    browser_type = project_data.browser if project_data and project_data.browser else 'chromium'
                    headless = True  # 服务端始终用 headless 模式
            else:
                project_data = await crud.get_project(batch_db, project_id)
                browser_type = project_data.browser if project_data and project_data.browser else 'chromium'
                headless = True  # 服务端始终用 headless 模式
        except SQLAlchemyError:
            logger.warning("Failed to load environment/project settings; falling back to defaults", exc_info=True)
            browser_type = 'chromium'
            headless = True

        # 先预创建所有 pending TestRun 记录（在浏览器启动之前），
        # 确保即使浏览器启动失败，报告页面也能看到用例执行记录
        precreated_run_ids = await precreate_pending_runs(
            batch_db, case_ids, batch_id, init_case_ids=init_case_ids
        )

        # 创建或复用浏览器
        try:
            async def _factory():
                mgr = PlaywrightMCPManager(browser_type=browser_type, headless=headless)
                await mgr.start()
                return mgr

            existing = await browser_pool.get_or_create(project_id, _factory)
            if existing is not None:
                mcp_manager = existing
            else:
                mcp_manager = await _factory()
                await browser_pool.register(project_id, mcp_manager)
        except Exception as exc:  # noqa: BLE001 - 见下方注释
            # Broad catch is necessary: PlaywrightMCPManager.start spawns an npx
            # subprocess, opens stdio pipes, and talks to a Playwright MCP server.
            # Failures can surface as OSError (subprocess), ConnectionError, or
            # asyncio.TimeoutError — any of them must be reported as a clean
            # "browser startup failed" so all pre-created pending TestRun
            # records get marked as failed consistently.
            logger.exception("Failed to start browser for batch %s", batch_id)
            for cid in ((init_case_ids or []) + case_ids):
                await _record_batch_case_failure(
                    batch_db, precreated_run_ids, cid, batch_id,
                    message=f"Browser startup failed: {exc}",
                )
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
                logger.info("Batch init-case %s finished: %s", case_id, result['status'])
            except Exception as exc:  # noqa: BLE001 - 见下方注释
                # Broad catch is necessary: run_test_case_in_browser touches DB,
                # MCP stdio, LLM HTTP, JSON parsing, and assertions. Any
                # unhandled error must be recorded on the pre-created TestRun
                # row so the report page stays consistent.
                logger.exception("Batch init-case %s failed", case_id)
                await _record_batch_case_failure(
                    batch_db, precreated_run_ids, case_id, batch_id,
                    message=f"Batch init-case executor exception: {exc}",
                )
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
            except Exception as exc:  # noqa: BLE001 - 见下方注释
                # Broad catch is necessary: run_test_case_in_browser touches DB,
                # MCP stdio, LLM HTTP, JSON parsing, and assertions. Any
                # unhandled error must be recorded on the pre-created TestRun
                # row so the report page stays consistent.
                logger.exception(
                    "Batch: case %s failed with exception",
                    case_id,
                )
                await _record_batch_case_failure(
                    batch_db, precreated_run_ids, case_id, batch_id,
                    message=f"Batch executor exception: {exc}",
                )
                results.append({
                    "case_id": case_id,
                    "status": "failed",
                    "error": str(exc),
                    "batch_id": batch_id,
                })

        return results
