# core/runner/_api_execution.py
"""服务端接口用例执行与报告落库（029-api-testing T040/T041）。

case_kind='api' 的用例由 core.runner._orchestrator 分发到本模块：
1. 加载 TestCase.api_spec 与执行环境（environment_id → environments 表，
   无则回退项目 base_url），组装 ``{"id","name","base_url","variables","headers"}``
2. 调 ``core.api_runner.runner.run_api_case``（httpx 发送，永不抛异常）
3. 落库前脱敏/截断：request.headers 经 mask_headers；response.body 截断前 100KB；
   断言 expected/actual 中 secret 变量值经 mask_secrets_in_text
4. 写 report.json（reports/run_{case_id}_{uid}/report.json，steps 为 StepDetail 超集）
5. save_run_results 落库：test_runs + run_logs 逐步骤 + 批次计数

约束：不修改 core/api_runner/*；不发送真实网络请求（client 可注入 MockTransport）。
"""
from __future__ import annotations

import asyncio
import json as _json
import logging
import os
import uuid
from typing import Any, Optional

from app import crud
from app.tz import now as tz_now

from core.api_runner.variables import is_secret_name, mask_headers, mask_secrets_in_text, mask_value
from core.runner._persistence import save_run_results

logger = logging.getLogger(__name__)

RESPONSE_BODY_LIMIT = 100 * 1024  # 100KB
TRUNCATED_SUFFIX = "...[TRUNCATED]"


def _write_report_sync(path: str, report: dict) -> None:
    """同步写报告 JSON 到磁盘（供 asyncio.to_thread 包装）。"""
    with open(path, "w", encoding="utf-8") as f:
        _json.dump(report, f, ensure_ascii=False, indent=2)


async def _load_api_environment(
    db, environment_id: int | None, project_id: int
) -> Optional[dict]:
    """环境快照 ``{"id","name","base_url","variables","headers"}``。

    environment_id 提供且存在 → 取环境；否则回退项目 base_url。
    """
    if environment_id:
        try:
            env = await crud.get_environment(db, environment_id)
        except Exception:  # noqa: BLE001 - 环境查询失败回退项目 base_url
            logger.warning("环境查询失败 env_id=%s", environment_id, exc_info=True)
            env = None
        if env is not None:
            return {
                "id": env.id,
                "name": env.name,
                "base_url": env.base_url or "",
                "variables": env.variables or [],
                "headers": env.headers or [],
            }
    try:
        project = await crud.get_project(db, project_id)
    except Exception:  # noqa: BLE001 - 项目查询失败返回 None（无 base_url）
        logger.warning("项目查询失败 project_id=%s", project_id, exc_info=True)
        project = None
    if project is not None and project.base_url:
        return {
            "id": None,
            "name": None,
            "base_url": project.base_url,
            "variables": [],
            "headers": [],
        }
    return None


async def _persist_execution_mode(db: AsyncSession, run_id: int, execution_mode: str) -> None:
    """把执行位置写进 test_runs.execution_mode（报告页据此标注"客户端执行"）。"""
    from sqlalchemy import update as sa_update

    from app import db_models

    try:
        await db.execute(
            sa_update(db_models.TestRun)
            .where(db_models.TestRun.id == run_id)
            .values(execution_mode=execution_mode)
        )
        await db.commit()
    except Exception:
        # 位置标注失败不影响用例结果（报告少一个标签而已）
        logger.warning("写入 execution_mode 失败 run_id=%s mode=%s", run_id, execution_mode, exc_info=True)


def _collect_secret_values(api_spec: dict, environment: Optional[dict]) -> list[str]:
    """api_spec.variables 与环境 variables 中 secret=true 的值（断言文本脱敏用）。"""
    secrets: list[str] = []
    for items in (api_spec.get("variables"), (environment or {}).get("variables")):
        for it in items or []:
            if isinstance(it, dict) and it.get("secret"):
                value = it.get("value")
                if value is not None and str(value):
                    secrets.append(str(value))
    return secrets


def _truncate_body(body: Any) -> Any:
    """响应体截断到前 100KB，超过追加 ``...[TRUNCATED]``。"""
    if body is None:
        return None
    text = str(body)
    if len(text) <= RESPONSE_BODY_LIMIT:
        return text
    return text[:RESPONSE_BODY_LIMIT] + TRUNCATED_SUFFIX


def _sanitize_steps(steps: list[dict], secret_values: list[str]) -> list[dict]:
    """落库前脱敏：request.headers 再 mask_headers；response.body 截断；
    断言 expected/actual 中 secret 值 mask；补 StepDetail 契约字段。"""
    out: list[dict] = []
    for step in steps:
        s = dict(step)
        if secret_values:
            s["description"] = mask_secrets_in_text(s.get("description"), secret_values)
            s["error"] = mask_secrets_in_text(s.get("error"), secret_values)
        req = s.get("request")
        if isinstance(req, dict) and req.get("headers") is not None:
            s["request"] = {**req, "headers": mask_headers(req["headers"])}
        resp = s.get("response")
        if isinstance(resp, dict) and resp.get("body") is not None:
            s["response"] = {**resp, "body": _truncate_body(resp["body"])}
        assertions = s.get("assertions")
        if isinstance(assertions, list) and secret_values:
            masked: list[Any] = []
            for a in assertions:
                if isinstance(a, dict):
                    masked.append({
                        **a,
                        "expected": mask_secrets_in_text(a.get("expected"), secret_values),
                        "actual": mask_secrets_in_text(a.get("actual"), secret_values),
                        "error": mask_secrets_in_text(a.get("error"), secret_values),
                    })
                else:
                    masked.append(a)
            s["assertions"] = masked
        extracted = s.get("extracted")
        if isinstance(extracted, list):
            # 提取值同样要脱敏：token/password 类变量写进报告等于把凭据留档
            s["extracted"] = [
                {**e, "value": mask_value(e.get("value"))}
                if isinstance(e, dict) and is_secret_name(e.get("variable"))
                else e
                for e in extracted
            ]
        # StepDetail 超集契约：screenshot_path 必填（api 用例无截图）
        s.setdefault("screenshot_path", None)
        out.append(s)
    return out


def _first_failure_error(steps: list[dict]) -> Optional[str]:
    """第一条失败步骤的 error（整体失败时作为 test_run 失败原因）。"""
    for step in steps:
        if step.get("status") == "failed" and step.get("error"):
            return str(step["error"])
    return None


async def _writeback_environment_variables(
    db, env_id: int, updates: dict[str, str]
) -> None:
    """把 environment 作用域提取写回 environments.variables。

    写回失败只记 warning，不影响用例结果（T038）。
    """
    try:
        await crud.update_environment_variables(db, env_id, updates)
    except Exception:  # noqa: BLE001 - 写回失败不影响用例结果
        logger.warning("环境变量写回失败 env_id=%s", env_id, exc_info=True)


async def run_api_case_server(
    case_id: int,
    *,
    db,
    batch_id: int | None = None,
    run_id: int | None = None,
    environment_id: int | None = None,
    client=None,
    case_variables: Optional[list[dict]] = None,
) -> dict:
    """服务端执行一条接口用例并落库（test_runs + run_logs + report.json）。

    ``client`` 仅供测试注入 httpx.MockTransport；None 时 run_api_case 内部自建。
    ``case_variables`` 场景级变量覆盖（存在时覆盖用例自身同名变量）。
    """
    from core.api_runner.runner import run_api_case

    start_time = tz_now()
    try:
        tc = await crud.get_test_case(db, case_id)
    except Exception:  # noqa: BLE001 - 查询失败按用例不存在处理
        logger.exception("加载用例 %s 失败", case_id)
        tc = None
    if tc is None:
        error = f"用例 {case_id} 不存在"
        end_time = tz_now()
        await save_run_results(
            case_id, "failed", start_time, end_time,
            (end_time - start_time).total_seconds(),
            None, None,
            [{"step_id": None, "level": "ERROR", "message": error, "screenshot_path": None}],
            batch_id=batch_id, run_id=run_id,
        )
        return {"case_id": case_id, "status": "failed", "error": error, "batch_id": batch_id}

    api_spec = tc.api_spec or {}
    environment = await _load_api_environment(db, environment_id, tc.project_id)

    # T050：执行位置（服务端 / 客户端 Agent）；环境级配置优先于用例级
    execution_mode = str(
        (environment or {}).get("execution_mode") or api_spec.get("execution_mode") or "server"
    ).strip()
    # T047：绑定数据集时按行迭代 —— 执行侧按 dataset_id 取快照传给执行器
    dataset = None
    dataset_id = api_spec.get("dataset_id")
    if dataset_id:
        try:
            from app.crud import api_dataset as crud_dataset

            ds = await crud_dataset.get_dataset(db, int(dataset_id))
        except Exception:
            logger.warning("加载数据集失败 dataset_id=%s", dataset_id, exc_info=True)
            ds = None
        if ds is None:
            logger.warning("数据集不存在，按单次执行 dataset_id=%s", dataset_id)
        else:
            dataset = {"id": ds.id, "columns": list(ds.columns or []), "rows": list(ds.rows or [])}

    result = await run_api_case(
        api_spec,
        environment=environment,
        case_variables=case_variables,
        client=client,
        execution_mode=execution_mode,
        dataset=dataset,
    )

    # T038：environment 作用域提取 → 用例成功后写回 environments.variables
    if (
        result.status == "passed"
        and result.environment_variables
        and environment is not None
        and environment.get("id") is not None
    ):
        await _writeback_environment_variables(db, environment["id"], result.environment_variables)

    if run_id and execution_mode != "server":
        await _persist_execution_mode(db, run_id, execution_mode)

    secret_values = _collect_secret_values(api_spec, environment)
    steps = _sanitize_steps(result.steps, secret_values)

    status = result.status
    error = result.error or (_first_failure_error(steps) if status == "failed" else None)
    if error:
        error = mask_secrets_in_text(error, secret_values)

    # report.json（复用 reports/run_{case_id}_{uid} 目录约定）
    run_uid = uuid.uuid4().hex[:12]
    output_dir = os.path.join("reports", f"run_{case_id}_{run_uid}")
    await asyncio.to_thread(os.makedirs, output_dir, exist_ok=True)
    report_path = os.path.join(output_dir, "report.json")
    report = {
        "case_id": case_id,
        "status": status,
        "started_at": start_time.isoformat(),
        "duration_ms": result.duration_ms,
        "environment": (
            {"id": environment.get("id"), "name": environment.get("name")}
            if environment else None
        ),
        "variables_used": sorted(result.variables.keys()),
        "steps": steps,
    }
    await asyncio.to_thread(_write_report_sync, report_path, report)

    # run_logs 逐步骤（level=success/error，message=description，失败附 error）
    logs: list[dict] = []
    for step in steps:
        message = str(step.get("description") or "")
        if not step.get("success") and step.get("error"):
            message = f"{message} | {step['error']}"
        logs.append({
            # 接口用例的步骤在 api_spec 里，没有 test_steps 行 → step_id 必须为空，
            # 传 step_number 会触发 run_logs.step_id 外键违例（真机实测过）
            "step_id": None,
            "level": "success" if step.get("success") else "error",
            "message": message,
            "screenshot_path": None,
        })
    if status == "failed" and error:
        logs.append({
            "step_id": None,
            "level": "ERROR",
            "message": f"用例失败: {error}",
            "screenshot_path": None,
        })

    end_time = tz_now()
    saved_run_id = await save_run_results(
        case_id, status, start_time, end_time,
        (end_time - start_time).total_seconds(),
        report_path, None, logs,
        batch_id=batch_id, run_id=run_id,
    )

    return {
        "case_id": case_id,
        "status": status,
        "run_id": saved_run_id,
        "batch_id": batch_id,
        "error": error,
        "report_path": report_path,
        "steps": steps,
    }


__all__ = ["run_api_case_server"]