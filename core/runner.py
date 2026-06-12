# core/runner.py
"""
Test case execution engine using LLM + Playwright MCP.

Replaces browser-use Agent with:
  1. Playwright MCP server (npx @playwright/mcp@latest, stdio transport)
  2. LLM generates PlaywrightMCPToolCall from NL step + accessibility snapshot
  3. MCP client executes the tool call

Flow per step:
  browser_snapshot() → LLM generates tool call → MCP call_tool() → capture result
"""

import asyncio
import ipaddress
import json as _json
import logging
import os
import sys
import uuid
from datetime import datetime, timezone
from urllib.parse import urlparse
from app.tz import now as tz_now
from typing import Optional

_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, _project_root)

from app import crud
from app.database import SessionLocal
from core.llm_wrapper import create_openai_client, _resolve_config as _resolve_llm_config
from core.step_executor import execute_step_mcp

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# SSRF 防护 — 禁止导航到内网地址
# ---------------------------------------------------------------------------


_PRIVATE_NETS = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
]


def _validate_nav_url(url: str | None) -> str | None:
    """校验导航 URL，阻止 SSRF 到内网地址。返回 None 表示拒绝"""
    if not url:
        return None
    try:
        parsed = urlparse(url)
        host = parsed.hostname
        if not host:
            return url
        # 阻止空主机名、localhost 变体
        if host in ("localhost", "localhost.localdomain", "127.0.0.1", "0.0.0.0", "::1", "[::1]"):
            logger.warning(f"SSRF 防护: 拒绝 localhost URL: {url}")
            return None
        try:
            addr = ipaddress.ip_address(host)
            for net in _PRIVATE_NETS:
                if addr in net:
                    logger.warning(f"SSRF 防护: 拒绝内网地址: {url}")
                    return None
        except ValueError:
            pass  # 域名，不做 IP 检查
        return url
    except Exception as exc:
        logger.warning(f"URL 校验异常: {url} -> {exc}")
        return None


# ---------------------------------------------------------------------------
# Auth cookie 注入
# ---------------------------------------------------------------------------


def _resolve_env_cookies(db, base_url_override: str | None) -> list[dict]:
    """根据 base_url_override 查找匹配的环境记录，返回该环境的 cookies 列表。

    没有 override / 找不到 / 没有 cookies → 返回空列表（不影响流程）。
    """
    if not base_url_override:
        return []
    try:
        from app import db_models
        env = (
            db.query(db_models.Environment)
            .filter(db_models.Environment.base_url == base_url_override)
            .order_by(db_models.Environment.is_default.desc(), db_models.Environment.id.asc())
            .first()
        )
        if not env:
            return []
        cookies = env.cookies
        if not cookies:
            return []
        if not isinstance(cookies, list):
            logger.warning(f"Environment {env.id} cookies 字段不是列表: {type(cookies).__name__}")
            return []
        return cookies
    except Exception as exc:
        logger.warning(f"读取环境 cookies 失败 (base_url={base_url_override}): {exc}")
        return []


async def _inject_auth_cookies(
    mcp_manager,
    cookies: list[dict],
    nav_url: str | None,
) -> int:
    """通过 MCP browser_set_cookie 注入 cookies 列表。

    每个 cookie 字典支持: {name, value, domain?, path?, expires?, httpOnly?, secure?, sameSite?}
    若 cookie 未指定 domain，从 nav_url 提取 hostname 作为默认 domain。

    返回成功注入的数量。任一 cookie 失败仅记录 warning，不抛出。
    """
    if not cookies:
        return 0

    default_domain: str | None = None
    if nav_url:
        try:
            default_domain = urlparse(nav_url).hostname
        except Exception:
            default_domain = None

    success_count = 0
    for cookie in cookies:
        if not isinstance(cookie, dict):
            logger.warning(f"跳过非法 cookie 项（非字典）: {cookie!r}")
            continue
        name = cookie.get("name")
        value = cookie.get("value", "")
        if not name:
            logger.warning(f"跳过缺少 name 的 cookie: {cookie!r}")
            continue

        args: dict = {
            "name": name,
            "value": str(value),
        }
        domain = cookie.get("domain") or default_domain
        if domain:
            args["domain"] = domain
        if cookie.get("path"):
            args["path"] = cookie["path"]
        elif "path" not in args:
            args["path"] = "/"
        for opt_key in ("expires", "httpOnly", "secure", "sameSite", "url"):
            if opt_key in cookie and cookie[opt_key] is not None:
                args[opt_key] = cookie[opt_key]

        try:
            result = await mcp_manager.call_tool("browser_set_cookie", args)
            if result.get("success"):
                success_count += 1
                logger.info(f"Cookie 注入成功: {name} @ {args.get('domain', '<no-domain>')}")
            else:
                logger.warning(
                    f"Cookie 注入失败: {name} -> {result.get('text') or result.get('error', 'unknown')}"
                )
        except Exception as exc:
            logger.warning(f"Cookie 注入异常: {name} -> {exc}")

    if success_count:
        logger.info(f"已注入 {success_count}/{len(cookies)} 个 cookies")
    return success_count


# ---------------------------------------------------------------------------
# DB persistence
# ---------------------------------------------------------------------------


def save_run_results(
    case_id: int,
    status: str,
    start_time: datetime,
    end_time: datetime,
    duration: float,
    report_path: Optional[str],
    log_path: Optional[str],
    logs: list[dict],
    batch_id: Optional[int] = None,
    run_id: Optional[int] = None,
    is_init: bool = False,
) -> int | None:
    db = SessionLocal()
    try:
        from app import db_models

        if run_id:
            db_run = db.query(db_models.TestRun).filter(db_models.TestRun.id == run_id).first()
            if db_run:
                db_run.status = status
                db_run.start_time = start_time
                db_run.end_time = end_time
                db_run.duration = duration
                db_run.report_path = report_path
                db_run.log_path = log_path
            else:
                run_id = None

        if not run_id:
            db_run = db_models.TestRun(
                case_id=case_id,
                batch_id=batch_id,
                status=status,
                start_time=start_time,
                end_time=end_time,
                duration=duration,
                report_path=report_path,
                log_path=log_path,
                is_init=is_init,
            )
            db.add(db_run)
            db.commit()
            db.refresh(db_run)

        for log_entry in logs:
            db_log = db_models.RunLog(
                run_id=db_run.id,
                step_id=log_entry.get('step_id'),
                level=log_entry['level'],
                message=log_entry['message'],
                screenshot_path=log_entry.get('screenshot_path'),
            )
            db.add(db_log)
        db.commit()
        logger.info(f"Test run results saved, run ID = {db_run.id}, batch_id = {batch_id}")

        if batch_id:
            crud.update_batch_counters(db, batch_id, status)
        return db_run.id
    except Exception as exc:
        db.rollback()
        logger.error(f"Failed to save run results: {exc}", exc_info=True)
    finally:
        db.close()


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
            import sqlalchemy as sa
            _stmt = (
                sa.update(db_models.TestRun)
                .where(db_models.TestRun.id == run_id)
                .values(status="running")
            )
            r = db.execute(_stmt)
            db.commit()
            if r.rowcount == 0:
                logger.warning(f"TestRun {run_id} not found, will create new record")
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
                logger.warning(f"Failed to navigate to {nav_url}: {nav_result.get('text')}")
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

        for idx, step in enumerate(step_list):
            logger.info(
                f"--- Step {step['step_order']}: {step['description']} ---"
            )

            result = await execute_step_mcp(
                step,
                mcp_manager,
                llm_client,
                model=resolved_model,
                step_timeout_ms=120000,
                screenshot_dir=screenshot_dir,
            )

            log_msg = (
                f"Step {step['step_order']} "
                f"{'✓' if result['success'] else '✗'}"
            )
            if result['error']:
                log_msg += f" — {result['error']}"
            logger.info(log_msg)

            run_log_entries.append({
                'step_id': step['id'],
                'level': 'ERROR' if not result['success'] else 'INFO',
                'message': (
                    result.get('error')
                    or f"Completed: {result.get('action', '')}"
                ),
                'screenshot_path': result.get('screenshot_path'),
            })

            step_results.append(result)

            if not result['success']:
                consecutive_failures += 1
            else:
                consecutive_failures = 0

            if consecutive_failures >= max_failures:
                failed_step_number = step['step_order']
                logger.warning(
                    f"步骤 {failed_step_number} 失败，跳过后续步骤"
                )
                for remaining in step_list[idx + 1:]:
                    step_results.append({
                        'step_number': remaining['step_order'],
                        'original_description': remaining['description'],
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
        test_status = "failed"
        logger.error(f"Error executing test case {case_id}: {exc}", exc_info=True)
        run_log_entries.append({
            "step_id": None,
            "level": "CRITICAL",
            "message": str(exc),
            "screenshot_path": None,
        })

    finally:
        end_time = tz_now()
        duration = (end_time - start_time).total_seconds()

        # 用 sqlalchemy update 直接更新 TestRun（避免 ORM session identity map 问题）
        if run_id is not None:
            try:
                import sqlalchemy as sa
                now = tz_now()
                stmt = (
                    sa.update(db_models.TestRun)
                    .where(db_models.TestRun.id == run_id)
                    .values(
                        status=test_status,
                        start_time=start_time,
                        end_time=end_time,
                        duration=duration,
                        report_path=case_report_path,
                        log_path=None,
                    )
                )
                db.execute(stmt)
                for log_entry in run_log_entries:
                    db_log = db_models.RunLog(
                        run_id=run_id,
                        step_id=log_entry.get('step_id'),
                        level=log_entry['level'],
                        message=log_entry['message'],
                        screenshot_path=log_entry.get('screenshot_path'),
                    )
                    db.add(db_log)
                db.commit()
                # 更新批次计数器（update_batch_counters 内部已提交）
                if batch_id:
                    try:
                        crud.update_batch_counters(db, batch_id, test_status)
                    except Exception as bexc:
                        logger.error(f"Failed to update batch counters: {bexc}")
                        db.rollback()
                logger.info(f"TestRun {run_id} updated -> {test_status}")
            except Exception as exc:
                logger.error(f"Failed to update TestRun {run_id}: {exc}")
                db.rollback()
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


async def run_test_case(case_id: int, batch_id: int | None = None, environment_id: int | None = None):
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
        except Exception as exc:
            logger.info(f"MCP stop after start failure: {exc}")
        return {"case_id": case_id, "status": "failed", "error": str(exc)}

    try:
        await run_test_case_in_browser(case_id, mcp_manager, batch_id=batch_id, base_url_override=base_url_override)
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(
        description="Run a test case with Playwright MCP."
    )
    parser.add_argument("case_id", type=int, help="Test case ID to execute")
    args = parser.parse_args()

    try:
        asyncio.run(run_test_case(args.case_id))
    except ValueError as exc:
        if "I/O operation on closed pipe" not in str(exc):
            raise
