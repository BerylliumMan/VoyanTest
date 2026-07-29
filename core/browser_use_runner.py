# core/browser_use_runner.py
"""Server-side browser-use case runner (DB + reporting).

Pure NL execution helpers live in ``core.browser_use_exec`` (safe for Agent client).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid

from app import crud
from app.database import AsyncSessionLocal
from app.tz import now as tz_now
from core.browser_use_exec import (
    build_step_task,
    create_browser_use_llm_from_config,
    execute_nl_steps_browser_use,
    history_to_step_fields,
)
from core.runner._persistence import (
    mark_run_running,
    save_run_results,
)
from core.runner._validators import _validate_nav_url

logger = logging.getLogger(__name__)

# Re-exports for back-compat
_build_step_task = build_step_task
_history_to_step_fields = history_to_step_fields


async def _create_browser_use_llm():
    """Build browser-use ChatOpenAI from VoyanTest AIConfig / execution agent."""
    from core.llm_wrapper import _resolve_config

    key, base, model = await _resolve_config()
    return create_browser_use_llm_from_config(api_key=key, api_base=base, model=model)


async def run_test_case_via_browser_use(
    case_id: int,
    *,
    batch_id: int | None = None,
    run_id: int | None = None,
    base_url_override: str | None = None,
    headless: bool = True,
    max_steps_per_nl: int = 20,
) -> dict:
    """Execute one test case with browser-use (shared browser session across steps)."""
    try:
        import browser_use  # noqa: F401
    except ImportError as exc:
        msg = (
            "browser-use 未安装。请在服务端执行: pip install browser-use "
            f"(detail: {exc})"
        )
        logger.error(msg)
        start = tz_now()
        await save_run_results(
            case_id, "failed", start, tz_now(), 0.0, None, None,
            [{"step_id": None, "level": "CRITICAL", "message": msg, "screenshot_path": None}],
            batch_id=batch_id, run_id=run_id,
        )
        return {"case_id": case_id, "status": "failed", "error": msg, "backend": "browser_use"}

    start_time = tz_now()
    step_results: list[dict] = []
    output_dir: str | None = None
    case_report_path: str | None = None
    test_status = "failed"

    async with AsyncSessionLocal() as db:
        from app import db_models

        case_data = await crud.get_test_case(db, case_id)
        if not case_data:
            raise ValueError(f"Test case with ID {case_id} not found.")

        if run_id is not None:
            if not await mark_run_running(db, run_id):
                run_id = None
        if run_id is None:
            pending = db_models.TestRun(
                case_id=case_id, batch_id=batch_id, status="running",
                start_time=start_time, end_time=start_time,
            )
            db.add(pending)
            await db.commit()
            await db.refresh(pending)
            run_id = pending.id

        project_data = await crud.get_project(db, case_data.project_id)
        if not project_data:
            raise ValueError(f"Project with ID {case_data.project_id} not found.")

        nav_url = _validate_nav_url(base_url_override or project_data.base_url)
        steps_raw = await crud.get_steps_for_case(db, case_id)
        step_list = sorted(
            [
                {
                    "id": s.id,
                    "step_order": s.step_order,
                    "description": s.description or "",
                    "expected_result": s.parsed_result,
                }
                for s in steps_raw
            ],
            key=lambda x: x["step_order"],
        )
        if not step_list:
            raise ValueError("Test case has no steps")

        run_uid = uuid.uuid4().hex[:12]
        output_dir = os.path.join("reports", f"run_{case_id}_{run_uid}_bu")
        await asyncio.to_thread(os.makedirs, output_dir, exist_ok=True)
        log_file_path = os.path.join(output_dir, "run.log")

        case_logger = logging.getLogger(f"runner.browser_use.case_{case_id}")
        case_logger.setLevel(logging.INFO)
        case_logger.propagate = True
        file_handler = await asyncio.to_thread(logging.FileHandler, log_file_path, encoding="utf-8")
        file_handler.setFormatter(
            logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
        )
        case_logger.addHandler(file_handler)
        # Also capture core.browser_use_exec + library into the same file
        bu_exec_logger = logging.getLogger("core.browser_use_exec")
        bu_lib_logger = logging.getLogger("browser_use")
        bu_exec_logger.addHandler(file_handler)
        bu_lib_logger.addHandler(file_handler)

        try:
            case_logger.info(
                "Starting browser-use execution: case=%s name=%r steps=%s",
                case_id, case_data.name, len(step_list),
            )
            logger.info(
                "Starting browser-use execution: case=%s name=%r steps=%s",
                case_id, case_data.name, len(step_list),
            )

            llm = await _create_browser_use_llm()
            screenshots_dir = os.path.join(output_dir, "screenshots")

            def _progress(line: str) -> None:
                case_logger.info("%s", line)

            step_results = await execute_nl_steps_browser_use(
                step_list,
                llm=llm,
                base_url=nav_url,
                headless=headless,
                max_steps_per_nl=max_steps_per_nl,
                screenshots_dir=screenshots_dir,
                on_progress=_progress,
            )
            # Drop base64 blobs from report JSON (paths already set)
            for r in step_results:
                r.pop("screenshot_base64", None)

            test_status = (
                "passed" if step_results and all(r["success"] for r in step_results) else "failed"
            )
            report = {
                "test_case_id": case_id,
                "test_case_name": case_data.name,
                "status": test_status,
                "backend": "browser_use",
                "start_time": start_time.isoformat(),
                "end_time": tz_now().isoformat(),
                "duration": (tz_now() - start_time).total_seconds(),
                "steps": step_results,
            }
            case_report_path = os.path.join(output_dir, "report.json")
            await asyncio.to_thread(
                lambda: open(case_report_path, "w", encoding="utf-8").write(
                    json.dumps(report, ensure_ascii=False, indent=2)
                )
            )

            logs = []
            for r in step_results:
                logs.append({
                    "step_id": next(
                        (s["id"] for s in step_list if s["step_order"] == r["step_number"]),
                        None,
                    ),
                    "level": "INFO" if r["success"] else "ERROR",
                    "message": (
                        f"[browser-use] 步骤{r['step_number']}: "
                        + ("通过" if r["success"] else f"失败 — {r.get('error') or ''}")
                    ),
                    "screenshot_path": r.get("screenshot_path"),
                })

            end_time = tz_now()
            case_logger.info("browser-use finished status=%s", test_status)
            await save_run_results(
                case_id,
                test_status,
                start_time,
                end_time,
                (end_time - start_time).total_seconds(),
                case_report_path,
                log_file_path,
                logs,
                batch_id=batch_id,
                run_id=run_id,
            )
        finally:
            case_logger.removeHandler(file_handler)
            bu_exec_logger.removeHandler(file_handler)
            bu_lib_logger.removeHandler(file_handler)
            await asyncio.to_thread(file_handler.close)

    return {
        "case_id": case_id,
        "status": test_status,
        "backend": "browser_use",
        "report_path": case_report_path,
        "steps": step_results,
    }
