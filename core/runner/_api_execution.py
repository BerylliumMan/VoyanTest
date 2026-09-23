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
    case_id: int | None,
    *,
    db,
    batch_id: int | None = None,
    run_id: int | None = None,
    environment_id: int | None = None,
    client=None,
    case_variables: Optional[list[dict]] = None,
    spec_override: Optional[dict] = None,
    seed_runtime: Optional[dict] = None,
    display_name: Optional[str] = None,
    project_id: int | None = None,
) -> dict:
    """服务端执行一条接口用例并落库（test_runs + run_logs + report.json）。

    ``client`` 仅供测试注入 httpx.MockTransport；None 时 run_api_case 内部自建。
    传入已有 client 时执行器不会关闭它（场景共享 Cookie 用）。
    ``case_variables`` 场景级变量覆盖（存在时覆盖用例自身同名变量）。
    ``spec_override`` 用于场景里的自定义请求 / 引用定义，此时 case_id 可为空。
    ``seed_runtime`` 是上一步提取出的变量。
    """
    from core.api_runner.runner import run_api_case

    start_time = tz_now()
    tc = None
    if case_id is not None and spec_override is None:
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
                display_name=display_name,
            )
            return {
                "case_id": case_id,
                "status": "failed",
                "error": error,
                "batch_id": batch_id,
                "runtime": dict(seed_runtime or {}),
            }

    if spec_override is not None:
        api_spec = spec_override
        resolved_project = project_id if project_id is not None else (tc.project_id if tc else None)
    else:
        api_spec = (tc.api_spec or {}) if tc is not None else {}
        resolved_project = tc.project_id if tc is not None else project_id
    environment = await _load_api_environment(db, environment_id, resolved_project or 0)

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
        seed_runtime=seed_runtime,
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
    report_key = case_id if case_id is not None else "step"
    output_dir = os.path.join("reports", f"run_{report_key}_{run_uid}")
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
        display_name=display_name,
    )

    return {
        "case_id": case_id,
        "status": status,
        "run_id": saved_run_id,
        "batch_id": batch_id,
        "error": error,
        "report_path": report_path,
        "steps": steps,
        "runtime": dict(result.runtime or {}),
    }


def scenario_step_to_spec(step: dict) -> dict:
    """把场景里的 definition/request 步骤包成一条 api_spec。"""
    return {
        "schema_version": 1,
        "variables": [],
        "fail_policy": "fail_fast",
        "steps": [
            {
                "order": 1,
                "name": step.get("name") or "",
                "enable": True,
                "definition_id": step.get("definition_id"),
                "request": step.get("request") or {},
                "assertions": step.get("assertions") or [],
                "extractors": step.get("extractors") or [],
                "pre": step.get("pre") or [],
                "post": step.get("post") or [],
            }
        ],
    }


async def _persist_skipped_step(
    *,
    batch_id: int,
    case_id: int | None,
    display_name: str,
    reason: str,
    status: str = "skipped",
) -> None:
    start_time = tz_now()
    end_time = tz_now()
    run_uid = uuid.uuid4().hex[:12]
    key = case_id if case_id is not None else "step"
    output_dir = os.path.join("reports", f"run_{key}_{run_uid}")
    await asyncio.to_thread(os.makedirs, output_dir, exist_ok=True)
    report_path = os.path.join(output_dir, "report.json")
    report = {
        "case_id": case_id,
        "status": status,
        "started_at": start_time.isoformat(),
        "steps": [
            {
                "step_number": 1,
                "name": display_name,
                "description": reason,
                "status": status,
                "success": status == "passed",
                "error": reason,
                "request": None,
                "response": None,
                "assertions": [],
                "extracted": [],
                "screenshot_path": None,
            }
        ],
    }
    await asyncio.to_thread(_write_report_sync, report_path, report)
    await save_run_results(
        case_id,
        status,
        start_time,
        end_time,
        0.0,
        report_path,
        None,
        [{"step_id": None, "level": "warning", "message": reason, "screenshot_path": None}],
        batch_id=batch_id,
        display_name=display_name,
    )


def _enabled_steps(steps: list) -> list[dict]:
    return [s for s in steps or [] if isinstance(s, dict) and s.get("enabled", True)]


def _planned_run_count(steps: list, dataset_rows: dict[int, list[dict]]) -> int:
    total = 0
    for step in _enabled_steps(steps):
        kind = step.get("type") or "case"
        children = step.get("children") or []
        if kind == "loop":
            inner = _planned_run_count(children, dataset_rows)
            total += (inner or 1) * int(step.get("count") or 1)
        elif kind == "foreach":
            rows = dataset_rows.get(int(step.get("dataset_id") or 0), [])
            if not rows:
                total += 1
            else:
                inner = _planned_run_count(children, dataset_rows)
                total += (inner or 1) * len(rows)
        elif kind == "if":
            total += 1 + _planned_run_count(children, dataset_rows)
        else:
            total += 1
    return total


def _collect_dataset_ids(steps: list, acc: set[int]) -> None:
    for step in steps or []:
        if not isinstance(step, dict):
            continue
        if step.get("type") == "foreach":
            try:
                acc.add(int(step.get("dataset_id")))
            except (TypeError, ValueError):
                pass
        _collect_dataset_ids(step.get("children") or [], acc)


async def _load_dataset_rows(session, steps: list) -> dict[int, list[dict]]:
    ids: set[int] = set()
    _collect_dataset_ids(steps, ids)
    out: dict[int, list[dict]] = {}
    if not ids:
        return out
    from app.crud import api_dataset as crud_dataset

    for dataset_id in ids:
        ds = await crud_dataset.get_dataset(session, dataset_id)
        rows: list[dict] = []
        if ds is not None:
            columns = [str(c) for c in (ds.columns or [])]
            for raw in ds.rows or []:
                if isinstance(raw, dict):
                    rows.append({str(k): "" if v is None else str(v) for k, v in raw.items()})
                elif isinstance(raw, list):
                    rows.append({
                        columns[i]: "" if i >= len(raw) or raw[i] is None else str(raw[i])
                        for i in range(len(columns))
                    })
        out[dataset_id] = rows
    return out


def _lookup_var(name: str, runtime: dict[str, str], scenario_vars: list[dict]) -> str:
    if name in runtime:
        return runtime[name]
    for item in scenario_vars or []:
        if isinstance(item, dict) and str(item.get("key") or "") == name and item.get("enable", True):
            return "" if item.get("value") is None else str(item.get("value"))
    return ""


def _condition_met(step: dict, runtime: dict[str, str], scenario_vars: list[dict]) -> bool:
    actual = _lookup_var(str(step.get("variable") or ""), runtime, scenario_vars)
    expected = "" if step.get("expected") is None else str(step.get("expected"))
    if step.get("operator") == "contains":
        return expected in actual
    return actual == expected


async def _skip_planned(step: dict, *, batch_id: int, dataset_rows: dict[int, list[dict]], reason: str) -> None:
    count = _planned_run_count([step], dataset_rows)
    label = str(step.get("name") or step.get("type") or "步骤")
    for _ in range(max(1, count) if step.get("enabled", True) else 0):
        await _persist_skipped_step(
            batch_id=batch_id,
            case_id=step.get("case_id") if (step.get("type") or "case") == "case" else None,
            display_name=label,
            reason=reason,
        )


async def _execute_scenario_steps(
    steps: list,
    *,
    session,
    batch_id: int,
    environment_id,
    project_id: int,
    scenario_vars: list[dict],
    client,
    runtime: dict[str, str],
    continue_on_failure: bool,
    dataset_rows: dict[int, list[dict]],
    execution_control,
    scenario_id: int,
    skip_rest: bool = False,
) -> bool:
    """执行一组步骤。返回是否应跳过后续步骤。runtime 就地更新。"""
    for step in _enabled_steps(steps):
        await execution_control.wait_if_paused(batch_id)
        if execution_control.is_stopped(batch_id):
            logger.info("场景执行被停止 scenario=%s batch=%s", scenario_id, batch_id)
            return True
        kind = step.get("type") or "case"
        if skip_rest:
            await _skip_planned(
                step, batch_id=batch_id, dataset_rows=dataset_rows, reason="因前序步骤失败跳过"
            )
            continue
        if kind == "wait":
            ms = min(int(step.get("ms") or 0), 60000)
            await asyncio.sleep(ms / 1000)
            await _persist_skipped_step(
                batch_id=batch_id,
                case_id=None,
                display_name=str(step.get("name") or "等待"),
                reason=f"等待 {ms} ms",
                status="passed",
            )
            continue
        if kind == "loop":
            times = int(step.get("count") or 1)
            children = step.get("children") or []
            if not _enabled_steps(children):
                for _ in range(times):
                    await _persist_skipped_step(
                        batch_id=batch_id, case_id=None,
                        display_name=str(step.get("name") or "循环"),
                        reason="循环没有可执行的子步骤",
                    )
                continue
            for _ in range(times):
                skip_rest = await _execute_scenario_steps(
                    children, session=session, batch_id=batch_id, environment_id=environment_id,
                    project_id=project_id, scenario_vars=scenario_vars, client=client, runtime=runtime,
                    continue_on_failure=continue_on_failure, dataset_rows=dataset_rows,
                    execution_control=execution_control, scenario_id=scenario_id, skip_rest=skip_rest,
                )
            continue
        if kind == "foreach":
            rows = dataset_rows.get(int(step.get("dataset_id") or 0), [])
            children = step.get("children") or []
            if not rows or not _enabled_steps(children):
                await _persist_skipped_step(
                    batch_id=batch_id, case_id=None,
                    display_name=str(step.get("name") or "遍历"),
                    reason="数据集没有数据行" if not rows else "遍历没有可执行的子步骤",
                )
                continue
            for row in rows:
                scoped = dict(runtime)
                scoped.update(row)
                runtime.clear()
                runtime.update(scoped)
                skip_rest = await _execute_scenario_steps(
                    children, session=session, batch_id=batch_id, environment_id=environment_id,
                    project_id=project_id, scenario_vars=scenario_vars, client=client, runtime=runtime,
                    continue_on_failure=continue_on_failure, dataset_rows=dataset_rows,
                    execution_control=execution_control, scenario_id=scenario_id, skip_rest=skip_rest,
                )
            continue
        if kind == "if":
            ok = _condition_met(step, runtime, scenario_vars)
            await _persist_skipped_step(
                batch_id=batch_id, case_id=None,
                display_name=str(step.get("name") or "条件"),
                reason="条件成立" if ok else "条件不成立，跳过子步骤",
                status="passed" if ok else "skipped",
            )
            if ok:
                skip_rest = await _execute_scenario_steps(
                    step.get("children") or [], session=session, batch_id=batch_id,
                    environment_id=environment_id, project_id=project_id, scenario_vars=scenario_vars,
                    client=client, runtime=runtime, continue_on_failure=continue_on_failure,
                    dataset_rows=dataset_rows, execution_control=execution_control,
                    scenario_id=scenario_id, skip_rest=skip_rest,
                )
            else:
                for child in _enabled_steps(step.get("children") or []):
                    await _skip_planned(
                        child, batch_id=batch_id, dataset_rows=dataset_rows, reason="条件不成立"
                    )
            continue
        if kind == "case":
            case_id = int(step.get("case_id"))
            tc = await crud.get_test_case(session, case_id)
            label = getattr(tc, "name", None) or f"用例 #{case_id}"
            result = await run_api_case_server(
                case_id, db=session, batch_id=batch_id, environment_id=environment_id,
                case_variables=scenario_vars, client=client, seed_runtime=runtime, display_name=label,
            )
        else:
            label = str(step.get("name") or ("自定义请求" if kind == "request" else "接口请求"))
            result = await run_api_case_server(
                None, db=session, batch_id=batch_id, environment_id=environment_id,
                project_id=project_id, spec_override=scenario_step_to_spec(step),
                case_variables=scenario_vars, client=client, seed_runtime=runtime, display_name=label,
            )
        runtime.clear()
        runtime.update(result.get("runtime") or {})
        if result.get("status") == "failed" and not continue_on_failure:
            skip_rest = True
    return skip_rest


async def run_scenario_batch(
    scenario_id: int,
    batch_id: int,
    user_id: int | None,
    environment_override: int | None = None,
) -> None:
    """按场景步骤执行：可选共用客户端与 runtime，失败可继续或跳过后续步骤。"""
    import httpx

    from app import execution_control
    from app.database import AsyncSessionLocal
    from app.services.notifications import notify_batch_completed

    shared_client = None
    try:
        async with AsyncSessionLocal() as session:
            from app.crud import api_scenario as crud_scenario

            scenario = await crud_scenario.get_scenario(session, scenario_id)
            if scenario is None:
                logger.error("场景不存在 scenario=%s", scenario_id)
                return
            steps = [s for s in (scenario.steps or []) if isinstance(s, dict)]
            environment_id = (
                environment_override
                if environment_override is not None
                else scenario.environment_id
            )
            project_id = scenario.project_id
            scenario_vars = list(scenario.variables or [])
            share_cookie = bool(getattr(scenario, "share_cookie", False))
            continue_on_failure = bool(getattr(scenario, "continue_on_failure", False))
            if share_cookie:
                shared_client = httpx.AsyncClient(verify=True, timeout=60.0)

            dataset_rows = await _load_dataset_rows(session, steps)
            planned = _planned_run_count(steps, dataset_rows)
            from app import db_models as _models

            batch_row = await session.get(_models.RunBatch, batch_id)
            if batch_row is not None:
                batch_row.total_cases = planned
                await session.commit()

            runtime: dict[str, str] = {}
            await _execute_scenario_steps(
                steps,
                session=session,
                batch_id=batch_id,
                environment_id=environment_id,
                project_id=project_id,
                scenario_vars=scenario_vars,
                client=shared_client,
                runtime=runtime,
                continue_on_failure=continue_on_failure,
                dataset_rows=dataset_rows,
                execution_control=execution_control,
                scenario_id=scenario_id,
            )
    except Exception:
        logger.exception("场景执行异常 scenario=%s batch=%s", scenario_id, batch_id)
    finally:
        if shared_client is not None:
            await shared_client.aclose()
        try:
            async with AsyncSessionLocal() as session:
                from sqlalchemy import select as _select

                from app import db_models

                result = await session.execute(
                    _select(db_models.RunBatch).where(db_models.RunBatch.id == batch_id)
                )
                batch = result.scalar_one_or_none()
                if batch is not None and batch.status == "running":
                    done = (batch.passed or 0) + (batch.failed or 0)
                    if done < (batch.total_cases or 0):
                        batch.status = "cancelled"
                        await session.commit()
        except Exception:
            logger.exception("场景批次收尾失败 batch=%s", batch_id)
        if user_id:
            try:
                await notify_batch_completed(batch_id, user_id)
            except Exception:
                logger.exception("场景批次通知失败 batch=%s", batch_id)


__all__ = ["run_api_case_server", "run_scenario_batch"]