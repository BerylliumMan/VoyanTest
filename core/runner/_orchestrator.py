# core/runner/_orchestrator.py
"""用例编排层 — 创建浏览器/MCP 实例，调度单用例或批量执行。

职责：
    1. 单用例执行入口（run_test_case）— 创建 / 销毁浏览器
    2. 批量执行入口（run_batch_test_cases）— 共享浏览器 + 预创建
       pending TestRun + 统一批次跟踪

UI 用例仅走 AgentRunner OTA（run_test_case_via_agent）；OTA Agent 未就绪
（无启用工具）时明确失败，不回退 legacy MCP / browser_use。
API 用例（case_kind=api）短路到接口执行器。

DB 操作的 SQL 细节已经下沉到 core.runner._persistence（mark_run_failed
/ precreate_pending_runs / update_run_on_completion），本模块只负责
协调浏览器/用例循环 + 批量跟踪。
"""
import logging

from sqlalchemy.exc import SQLAlchemyError

from app.tz import now as tz_now

from app import crud
from app.database import AsyncSessionLocal

from core.runner._api_execution import run_api_case_server
from core.runner._persistence import (
    build_batch_execution_queue,
    mark_run_failed,
    precreate_pending_runs,
    save_run_results,
)

from core.agent_runner.runner import AgentRunner
from core.llm_wrapper import create_openai_client
from core.agent_ota import should_use_ota_agent

logger = logging.getLogger(__name__)

_OTA_NOT_READY_MSG = (
    "OTA Agent 未就绪：请配置并启用 execution AgentDefinition 的工具列表；"
    "服务端 UI 执行已不再回退 legacy MCP / browser_use。"
)


# ---------------------------------------------------------------------------
# 批量跟踪 helper（只在 _orchestrator 内复用，不下沉到 _persistence，
# 因为它依赖 precreated_run_ids 这一批处理特有的状态）
# ---------------------------------------------------------------------------


async def _record_batch_case_failure(
    db,
    run_id: int | None,
    batch_id: int,
    message: str,
) -> None:
    """批量执行中单条用例抛异常时，把预创建的 TestRun 标记为 failed。"""
    if run_id:
        await mark_run_failed(db, run_id, message, batch_id=batch_id)


# ---------------------------------------------------------------------------
# AgentRunner 条件分发的辅助函数（T011）
# ---------------------------------------------------------------------------


async def _should_use_agent_runner(agent_def) -> bool:
    """兼容旧调用；逻辑见 ``core.agent_ota.should_use_ota_agent``。"""
    return should_use_ota_agent(agent_def)


async def _fail_ota_unavailable(
    case_id: int,
    message: str,
    *,
    batch_id: int | None = None,
    run_id: int | None = None,
) -> dict:
    """OTA 不可用时写入 failed TestRun 并返回明确失败结果。"""
    start_time = tz_now()
    end_time = start_time
    logs = [{
        "step_id": None,
        "level": "CRITICAL",
        "message": message,
        "screenshot_path": None,
    }]
    try:
        saved_run_id = await save_run_results(
            case_id, "failed", start_time, end_time, 0.0,
            None, None, logs,
            batch_id=batch_id,
            run_id=run_id,
        )
    except Exception:
        logger.exception("写入 OTA 不可用失败记录失败 case_id=%s", case_id)
        saved_run_id = run_id
    return {
        "case_id": case_id,
        "status": "failed",
        "error": message,
        "run_id": saved_run_id,
        "batch_id": batch_id,
    }


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

    通过 AgentRunner 让 LLM 自主规划并执行；目标描述会包含用例步骤，
    并写入 TestRun / 批次计数。

    Returns:
        执行结果字典，或 None（表示 AgentRunner 路径不可用，调用方应明确失败）
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

    from app.crud.agent_run import create_agent_run, update_agent_run_status
    from app.models.schemas import AgentRunCreate

    run = await create_agent_run(db, AgentRunCreate(
        agent_definition_id=agent_def.id,
        case_id=case_id,
        goal={"goal": goal_text, "case_id": case_id},
    ))
    await update_agent_run_status(db, run.id, "running")

    runner = AgentRunner(
        mcp_manager=mcp_manager,
        goal=goal_text,
        llm_client=llm_client,
        model=model,
        base_url=base_url,
        db=db,
        run_id=run.id,
    )

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
    """Execute a UI test case via AgentRunner OTA + Playwright MCP.

    ``backend`` 保留兼容参数，服务端 UI 路径一律走 OTA，忽略 legacy / browser_use。
    ``run_id`` 用于调试模式等已预创建 TestRun 的场景，保证 WS 广播 id 一致。
    """
    from core.browser_pool import BrowserPool

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

    # ── API 用例分发：case_kind='api' 短路到接口执行器，不走浏览器 ──
    if case_kind == "api":
        return await _run_api_case_server_entry(
            case_id, batch_id, environment_id, run_id,
        )

    if project_id is not None:
        async with BrowserPool.project_lock(project_id):
            return await _run_test_case_unlocked(
                case_id, batch_id, browser_type, headless, base_url_override,
                debug_mode=debug_mode, run_id=run_id,
            )
    return await _run_test_case_unlocked(
        case_id, batch_id, browser_type, headless, base_url_override,
        debug_mode=debug_mode, run_id=run_id,
    )


async def _run_api_case_server_entry(
    case_id: int,
    batch_id: int | None,
    environment_id: int | None,
    run_id: int | None,
) -> dict:
    """单用例 api 分发入口：自开 session 调 run_api_case_server。

    case_kind='api' 的用例不走浏览器/OTA，直接短路到接口执行器。
    """
    async with AsyncSessionLocal() as _db:
        return await run_api_case_server(
            case_id, db=_db, batch_id=batch_id, run_id=run_id,
            environment_id=environment_id,
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
):
    from core.playwright_manager import PlaywrightMCPManager

    mcp_manager = PlaywrightMCPManager(
        browser_type=browser_type,
        headless=headless,
    )
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
        _agent_def = None
        try:
            from app.crud.agent_definition import get_active_by_type as _get_active
            async with AsyncSessionLocal() as _agent_db:
                _agent_def = await _get_active(_agent_db, "execution")
        except Exception:
            logger.warning("加载 execution AgentDefinition 失败", exc_info=True)

        if not await _should_use_agent_runner(_agent_def):
            return await _fail_ota_unavailable(
                case_id, _OTA_NOT_READY_MSG,
                batch_id=batch_id, run_id=run_id,
            )

        async with AsyncSessionLocal() as _agent_db:
            result = await run_test_case_via_agent(
                case_id, mcp_manager, _agent_db, _agent_def,
                base_url=base_url_override,
                batch_id=batch_id,
                run_id=run_id,
            )
        if result is None:
            return await _fail_ota_unavailable(
                case_id,
                "OTA AgentRunner 不可用（LLM 客户端创建失败或 Agent 未就绪）",
                batch_id=batch_id, run_id=run_id,
            )
        logger.info("AgentRunner 完成 case_id=%s status=%s", case_id, result.get("status", "N/A"))
        return result
    except Exception:  # noqa: BLE001
        logger.exception("Unhandled error in OTA run_test_case for case %s", case_id)
        return await _fail_ota_unavailable(
            case_id, "OTA 执行发生未处理异常",
            batch_id=batch_id, run_id=run_id,
        )
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
    init_policy: str = "once",
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
        IDs of initialization cases.
    init_policy : str
        ``once`` — run inits once at batch start；
        ``before_each`` — re-run inits before every main case.

    UI cases use AgentRunner OTA only. API cases use the API runner.
    Init-case failure aborts remaining cases. Browser cleanup happens in finally.
    """
    if browser_pool is None:
        from core.browser_pool import BrowserPool as browser_pool

    from core.browser_pool import BrowserPool
    from core.playwright_manager import PlaywrightMCPManager

    exec_queue = build_batch_execution_queue(case_ids, init_case_ids, init_policy)
    total_cases = len(exec_queue)

    # 创建批次（如果未提供 batch_id）
    if batch_id is None:
        async with AsyncSessionLocal() as _db:
            batch = await crud.create_run_batch(_db, project_id, total_cases=total_cases, triggered_by=triggered_by)
            batch_id = batch.id

    mcp_manager = None
    base_url_override = None

    import asyncio
    from app import execution_control
    _cur = asyncio.current_task()
    if _cur is not None and batch_id is not None:
        await execution_control.register_batch_task(batch_id, _cur)

    try:
        async with BrowserPool.project_lock(project_id), AsyncSessionLocal() as batch_db:
            # ── AgentRunner OTA 就绪检查 ────────────────────────────────────
            agent_def = None
            use_agent_runner = False
            agent_llm_client = None
            agent_init_error: str | None = None
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
                else:
                    agent_init_error = _OTA_NOT_READY_MSG
            except Exception as exc:
                agent_init_error = f"OTA AgentRunner 初始化失败: {exc}"
                logger.warning("%s", agent_init_error, exc_info=True)

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
            precreated_runs = await precreate_pending_runs(
                batch_db, case_ids, batch_id,
                init_case_ids=init_case_ids, init_policy=init_policy,
            )

            # ── API 用例分发：加载 case_kind 映射 ──────────────────
            # case_kind='api' 的用例短路到 run_api_case_server，不走浏览器；
            # 全部为 api 时跳过浏览器创建（避免无浏览器环境启动失败）。
            case_kinds: dict[int, str] = {}
            try:
                for _cid, _rid, _is_init in precreated_runs:
                    _tc = await crud.get_test_case(batch_db, _cid)
                    case_kinds[_cid] = (
                        getattr(_tc, "case_kind", None) or "functional"
                        if _tc is not None else "functional"
                    )
            except Exception:  # noqa: BLE001 - 映射加载失败回退浏览器路径
                logger.warning("加载 case_kind 映射失败，api 用例将走浏览器路径", exc_info=True)
            all_api = bool(precreated_runs) and all(
                case_kinds.get(cid, "functional") == "api"
                for cid, _, _ in precreated_runs
            )
            has_ui = any(
                case_kinds.get(cid, "functional") != "api"
                for cid, _, _ in precreated_runs
            )

            async def _abort_remaining_from(start_idx: int, message: str) -> None:
                for j in range(start_idx, len(precreated_runs)):
                    _, rid, _ = precreated_runs[j]
                    await mark_run_failed(batch_db, rid, message, batch_id=batch_id)

            # UI 用例需要 OTA；未就绪则明确失败，不回退 legacy
            if has_ui and not use_agent_runner:
                fail_msg = agent_init_error or _OTA_NOT_READY_MSG
                results = []
                for case_id, _rid, is_init in precreated_runs:
                    if case_kinds.get(case_id) == "api":
                        result = await run_api_case_server(
                            case_id, db=batch_db, batch_id=batch_id, run_id=_rid,
                            environment_id=environment_id,
                        )
                    else:
                        await _record_batch_case_failure(
                            batch_db, _rid, batch_id, message=fail_msg,
                        )
                        result = {
                            "case_id": case_id,
                            "status": "failed",
                            "error": fail_msg,
                            "batch_id": batch_id,
                        }
                    results.append(result)
                return results

            # 创建或复用浏览器（全部为 api 用例时跳过，无需浏览器）
            if all_api:
                mcp_manager = None
            else:
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
                    for _cid, _rid, _is_init in precreated_runs:
                        await _record_batch_case_failure(
                            batch_db, _rid, batch_id,
                            message=f"Browser startup failed: {exc}",
                        )
                    return

                # Clear cookies once at batch start
                await mcp_manager.clear_cookies()

            results = []
            for idx, (case_id, _rid, is_init) in enumerate(precreated_runs):
                from app import execution_control
                await execution_control.wait_if_paused(batch_id)
                if execution_control.is_stopped(batch_id):
                    await crud.cancel_remaining_batch_runs(
                        batch_db, batch_id, message="用户停止执行",
                    )
                    await batch_db.commit()
                    break
                try:
                    if case_kinds.get(case_id) == "api":
                        result = await run_api_case_server(
                            case_id, db=batch_db, batch_id=batch_id, run_id=_rid,
                            environment_id=environment_id,
                        )
                    else:
                        result = await run_test_case_via_agent(
                            case_id, mcp_manager, batch_db, agent_def,
                            llm_client=agent_llm_client,
                            base_url=base_url_override,
                            batch_id=batch_id,
                            run_id=_rid,
                        )
                        if result is None:
                            fail_msg = "OTA AgentRunner 不可用（执行中返回 None）"
                            await _record_batch_case_failure(
                                batch_db, _rid, batch_id, message=fail_msg,
                            )
                            result = {
                                "case_id": case_id,
                                "status": "failed",
                                "error": fail_msg,
                                "batch_id": batch_id,
                            }
                    results.append(result)
                    status = result.get("status") if isinstance(result, dict) else None
                    logger.info(
                        "Batch case %s finished: %s (init=%s policy=%s)",
                        case_id, status, is_init, init_policy,
                    )
                    if is_init and status == "failed":
                        logger.warning(
                            "Init case %s failed — abort remaining in batch %s",
                            case_id, batch_id,
                        )
                        await _abort_remaining_from(
                            idx + 1, f"因初始化用例 {case_id} 失败而跳过",
                        )
                        break
                except Exception as exc:  # noqa: BLE001 - 见下方注释
                    # Broad catch: OTA / API / DB 任一未处理异常都要落到预创建 TestRun
                    label = "init-case" if is_init else "case"
                    logger.exception("Batch %s %s failed with exception", label, case_id)
                    await _record_batch_case_failure(
                        batch_db, _rid, batch_id,
                        message=f"Batch {label} executor exception: {exc}",
                    )
                    results.append({
                        "case_id": case_id,
                        "status": "failed",
                        "error": str(exc),
                        "batch_id": batch_id,
                    })
                    if is_init:
                        await _abort_remaining_from(
                            idx + 1, f"因初始化用例 {case_id} 失败而跳过",
                        )
                        break

            return results
    finally:
        if batch_id is not None:
            await execution_control.clear_batch(batch_id)
