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

from core.agent_runner.runner import AgentRunner
from core.llm_wrapper import create_openai_client
from core.agent_ota import should_use_ota_agent

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
# AgentRunner 条件分发的辅助函数（T011）
# ---------------------------------------------------------------------------


async def _should_use_agent_runner(agent_def) -> bool:
    """兼容旧调用；逻辑见 ``core.agent_ota.should_use_ota_agent``。"""
    return should_use_ota_agent(agent_def)


async def run_test_case_via_agent(
    case_id: int,
    mcp_manager,
    db,
    agent_def,
    *,
    llm_client=None,
    base_url: str | None = None,
    batch_id: int | None = None,
    run_id: int | None = None,
) -> dict | None:
    """使用 AgentRunner OTA 循环执行单条测试用例。

    该函数替代 run_test_case_in_browser，通过 AgentRunner 让 LLM 自主
    规划并执行；目标描述会包含用例步骤，并写入 TestRun / 批次计数。

    Returns:
        执行结果字典，或 None（表示 AgentRunner 路径不可用）
    """
    if not await _should_use_agent_runner(agent_def):
        return None

    start_time = tz_now()
    goal_text = str(agent_def.goal or "执行测试用例")
    try:
        tc = await crud.get_test_case(db, case_id)
        if tc:
            goal_text = f"测试用例: {tc.name}"
            if tc.description:
                goal_text += f"\n描述: {tc.description}"
            steps = list(getattr(tc, "steps", None) or [])
            steps.sort(key=lambda s: getattr(s, "step_order", 0) or 0)
            if steps:
                goal_text += "\n\n请严格按以下测试步骤执行（可观测预期写在括号内）："
                for s in steps:
                    line = f"\n{getattr(s, 'step_order', '?')}. {getattr(s, 'description', '') or ''}"
                    expected = (getattr(s, "parsed_result", None) or "").strip()
                    if expected:
                        line += f"（预期: {expected}）"
                    goal_text += line
    except Exception as exc:
        logger.warning("查询测试用例 %s 信息失败: %s", case_id, exc)

    if llm_client is None:
        try:
            llm_client = await create_openai_client(agent_type="execution")
        except Exception:
            logger.exception("无法创建 AgentRunner LLM 客户端")
            return None

    model = (agent_def.llm_config or {}).get("model") or None
    if isinstance(model, str) and not model.strip():
        model = None

    runner = AgentRunner(
        mcp_manager=mcp_manager,
        goal=goal_text,
        llm_client=llm_client,
        model=model,
        base_url=base_url,
    )

    from app.crud.agent_run import create_agent_run, update_agent_run_status
    from app.models.schemas import AgentRunCreate

    run = await create_agent_run(db, AgentRunCreate(
        agent_definition_id=agent_def.id,
        case_id=case_id,
        goal={"goal": goal_text, "case_id": case_id},
    ))
    await update_agent_run_status(db, run.id, "running")

    try:
        ota_result = await runner.run()
    except Exception as exc:  # noqa: BLE001
        logger.exception("AgentRunner 执行异常 (case_id=%s)", case_id)
        ota_result = {
            "status": "error",
            "turns_used": 0,
            "error": str(exc),
            "result": None,
        }

    final_status = ota_result.get("status", "failed")
    await update_agent_run_status(
        db, run.id, final_status,
        result=ota_result,
        turns_used=ota_result.get("turns_used", 0),
        error=ota_result.get("error"),
    )

    case_status = (
        "passed"
        if final_status in ("success", "passed", "completed", "ok")
        else "failed"
    )
    end_time = tz_now()
    logs = [{
        "step_id": None,
        "level": "INFO" if case_status == "passed" else "ERROR",
        "message": (
            f"AgentRunner 完成: status={final_status}, "
            f"turns={ota_result.get('turns_used', 0)}"
            + (f", error={ota_result.get('error')}" if ota_result.get("error") else "")
        ),
        "screenshot_path": None,
    }]
    try:
        saved_run_id = await save_run_results(
            case_id, case_status, start_time, end_time,
            (end_time - start_time).total_seconds(),
            None, None, logs,
            batch_id=batch_id,
            run_id=run_id,
        )
    except Exception:
        logger.exception("AgentRunner 写入 TestRun 失败 case_id=%s", case_id)
        saved_run_id = run_id

    return {
        "case_id": case_id,
        "status": case_status,
        "agent_run_id": run.id,
        "run_id": saved_run_id,
        "batch_id": batch_id,
        "turns_used": ota_result.get("turns_used", 0),
        "result": ota_result.get("result"),
        "error": ota_result.get("error"),
    }


# ---------------------------------------------------------------------------
# 单用例入口
# ---------------------------------------------------------------------------


async def run_test_case(
    case_id: int,
    batch_id: int | None = None,
    environment_id: int | None = None,
    debug_mode: bool = False,
    run_id: int | None = None,
    backend: str | None = None,
):
    """Execute a test case using LLM + Playwright MCP (npx subprocess).

    Backward-compatible wrapper that creates its own PlaywrightMCPManager.
    ``run_id`` 用于调试模式等已预创建 TestRun 的场景，保证 WS 广播 id 一致。
    ``backend`` 可选覆盖运行时配置：``playwright_mcp`` | ``browser_use`` | ``hybrid``。
    hybrid：MCP 优先；定位失败时同 CDP 挂 browser-use 救场一步（服务端与客户端一致）。
    """
    from core.browser_pool import BrowserPool
    from core.playwright_manager import PlaywrightMCPManager

    project_id: int | None = None
    case_kind: str | None = None
    browser_type = 'chromium'
    headless = True
    base_url_override = None
    async with AsyncSessionLocal() as _db:
        try:
            tc = await crud.get_test_case(_db, case_id)
            if tc:
                project_id = tc.project_id
                case_kind = getattr(tc, "case_kind", None) or "functional"
        except SQLAlchemyError:
            logger.warning("Failed to load case %s for project lock", case_id, exc_info=True)
        if environment_id:
            try:
                env = await crud.get_environment(_db, environment_id)
                if env:
                    browser_type = env.browser
                    headless = True  # 服务端始终用 headless 模式
                    base_url_override = env.base_url
            except SQLAlchemyError as exc:
                logger.warning("Environment lookup failed for env_id=%s: %s", environment_id, exc, exc_info=True)

    if project_id is not None:
        async with BrowserPool.project_lock(project_id):
            return await _run_test_case_unlocked(
                case_id, batch_id, browser_type, headless, base_url_override,
                debug_mode=debug_mode, run_id=run_id, backend=backend,
                case_kind=case_kind,
            )
    return await _run_test_case_unlocked(
        case_id, batch_id, browser_type, headless, base_url_override,
        debug_mode=debug_mode, run_id=run_id, backend=backend,
        case_kind=case_kind,
    )


async def _run_test_case_unlocked(
    case_id: int,
    batch_id: int | None,
    browser_type: str,
    headless: bool,
    base_url_override: str | None,
    *,
    debug_mode: bool = False,
    run_id: int | None = None,
    backend: str | None = None,
):
    from app.runtime_config import execution_backend_config
    from core.playwright_manager import PlaywrightMCPManager

    selected = (backend or execution_backend_config.backend or "playwright_mcp").strip()

    # Scheme B: optional browser-use backend (server-side only)
    if selected == "browser_use":
        from core.browser_use_runner import run_test_case_via_browser_use

        logger.info(
            "Using browser-use backend for case %s (max_steps_per_nl=%s)",
            case_id, execution_backend_config.max_steps_per_nl,
        )
        return await run_test_case_via_browser_use(
            case_id,
            batch_id=batch_id,
            run_id=run_id,
            base_url_override=base_url_override,
            headless=execution_backend_config.headless if execution_backend_config.headless is not None else headless,
            max_steps_per_nl=execution_backend_config.max_steps_per_nl,
        )

    mcp_manager = PlaywrightMCPManager(browser_type=browser_type, headless=headless)
    start_time = tz_now()
    try:
        await mcp_manager.start()
    except Exception as exc:  # noqa: BLE001
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
            run_id=run_id,
        )
        try:
            await mcp_manager.stop()
        except (OSError, RuntimeError) as stop_exc:
            logger.info("MCP stop after start failure: %s", stop_exc, exc_info=True)
        return {"case_id": case_id, "status": "failed", "error": str(exc)}

    try:
        try:
            from app.crud.agent_definition import get_active_by_type as _get_active
        except Exception:
            _get_active = None
        _agent_def = None
        if _get_active is not None:
            try:
                async with AsyncSessionLocal() as _agent_db:
                    _agent_def = await _get_active(_agent_db, "execution")
            except Exception:
                pass

        if await _should_use_agent_runner(_agent_def):
            async with AsyncSessionLocal() as _agent_db:
                result = await run_test_case_via_agent(
                    case_id, mcp_manager, _agent_db, _agent_def,
                    base_url=base_url_override,
                    batch_id=batch_id,
                    run_id=run_id,
                )
            logger.info("AgentRunner 完成 case_id=%s status=%s", case_id, (result or {}).get("status", "N/A"))
            if result is None:
                await run_test_case_in_browser(
                    case_id, mcp_manager,
                    batch_id=batch_id, run_id=run_id,
                    base_url_override=base_url_override, debug_mode=debug_mode,
                )
        else:
            await run_test_case_in_browser(
                case_id, mcp_manager,
                batch_id=batch_id, run_id=run_id,
                base_url_override=base_url_override, debug_mode=debug_mode,
            )
    except Exception:  # noqa: BLE001
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
    triggered_by: str | None = None,
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

    from core.browser_pool import BrowserPool
    from core.playwright_manager import PlaywrightMCPManager

    total_main = len(case_ids)
    total_init = len(init_case_ids) if init_case_ids else 0
    total_cases = total_main + total_init

    # 创建批次（如果未提供 batch_id）
    if batch_id is None:
        async with AsyncSessionLocal() as _db:
            batch = await crud.create_run_batch(_db, project_id, total_cases=total_cases, triggered_by=triggered_by)
            batch_id = batch.id

    mcp_manager = None
    base_url_override = None

    async with BrowserPool.project_lock(project_id), AsyncSessionLocal() as batch_db:
        # ── AgentRunner 分发检查（T011）────────────────────────────────────
        # 在打开浏览器之前，先检查是否有激活的 execution AgentDefinition。
        # 如果有且配置了工具，则使用 AgentRunner OTA 循环替代传统的
        # run_test_case_in_browser 逐步骤执行。
        agent_def = None
        use_agent_runner = False
        agent_llm_client = None
        try:
            from app.crud.agent_definition import get_active_by_type
            agent_def = await get_active_by_type(batch_db, "execution")
            if await _should_use_agent_runner(agent_def):
                agent_llm_client = await create_openai_client(agent_type="execution")
                use_agent_runner = True
                logger.info(
                    "AgentRunner 模式已激活: agent_def_id=%s, name=%s",
                    agent_def.id, agent_def.name,
                )
        except Exception as exc:
            logger.warning("AgentRunner 初始化失败，回退传统模式: %s", exc, exc_info=True)

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

        # Scheme B: browser-use batch — per-case session, skip Playwright MCP pool
        from app.runtime_config import execution_backend_config as _exec_backend
        if _exec_backend.backend == "browser_use":
            from core.browser_use_runner import run_test_case_via_browser_use

            results = []
            for case_id in (init_case_ids or []) + case_ids:
                _rid = precreated_run_ids.get(case_id)
                try:
                    result = await run_test_case_via_browser_use(
                        case_id,
                        batch_id=batch_id,
                        run_id=_rid,
                        base_url_override=base_url_override,
                        headless=_exec_backend.headless,
                        max_steps_per_nl=_exec_backend.max_steps_per_nl,
                    )
                    results.append(result)
                    logger.info(
                        "Batch browser-use case %s finished: %s",
                        case_id, (result or {}).get("status"),
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.exception("Batch browser-use case %s failed", case_id)
                    await _record_batch_case_failure(
                        batch_db, precreated_run_ids, case_id, batch_id,
                        message=f"browser-use executor exception: {exc}",
                    )
                    results.append({
                        "case_id": case_id,
                        "status": "failed",
                        "error": str(exc),
                        "batch_id": batch_id,
                        "backend": "browser_use",
                    })
            return results

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
                if use_agent_runner and agent_def is not None:
                    result = await run_test_case_via_agent(
                        case_id, mcp_manager, batch_db, agent_def,
                        llm_client=agent_llm_client,
                        base_url=base_url_override,
                        batch_id=batch_id,
                        run_id=_rid,
                    )
                    if result is None:
                        result = await run_test_case_in_browser(
                            case_id, mcp_manager, db=batch_db, clear_cookies=False,
                            batch_id=batch_id, run_id=_rid,
                            base_url_override=base_url_override,
                            debug_mode=debug_mode,
                        )
                else:
                    result = await run_test_case_in_browser(
                        case_id, mcp_manager, db=batch_db, clear_cookies=False,
                        batch_id=batch_id, run_id=_rid,
                        base_url_override=base_url_override,
                        debug_mode=debug_mode,
                    )
                results.append(result)
                logger.info("Batch init-case %s finished: %s", case_id, result['status'])
            except Exception as exc:  # noqa: BLE001
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
                if use_agent_runner and agent_def is not None:
                    result = await run_test_case_via_agent(
                        case_id, mcp_manager, batch_db, agent_def,
                        llm_client=agent_llm_client,
                        base_url=base_url_override,
                        batch_id=batch_id,
                        run_id=_rid,
                    )
                    if result is None:
                        result = await run_test_case_in_browser(
                            case_id, mcp_manager, db=batch_db, clear_cookies=False,
                            batch_id=batch_id, run_id=_rid,
                            base_url_override=base_url_override,
                            debug_mode=debug_mode,
                        )
                else:
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
