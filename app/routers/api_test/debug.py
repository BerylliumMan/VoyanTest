# app/routers/api_test/debug.py — 接口调试发送（029-api-testing 契约 §1.4）
#
# Postman 的「Send」：不建 test_runs / run_logs（常规执行链路才落库），只渲染并可选发送。
# dry_run=true 时只做变量渲染与校验（用于发送前缺失变量提示），且**不写请求历史**（US7 契约）。
# 真实发送（非 dry_run）写一条 api_request_history（成功/失败均记，见 _record_history）。
from __future__ import annotations

import json
import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app import db_models
from app.auth import get_current_user, get_user_project_filter
from app.database import get_async_db

logger = logging.getLogger(__name__)

router = APIRouter()

# UI 预览上限：报告侧是 100KB，调试面板只需要够看清结构
_BODY_PREVIEW_LIMIT = 2000


def _preview(text: Any) -> tuple[str, bool]:
    body = str(text or "")
    if len(body) > _BODY_PREVIEW_LIMIT:
        return body[:_BODY_PREVIEW_LIMIT] + "...[TRUNCATED]", True
    return body, False


def _ensure_project_access(user: Any, project_id: int) -> None:
    allowed = get_user_project_filter(user)
    if allowed is not None and project_id not in allowed:
        raise HTTPException(status_code=403, detail="无权访问该项目")


async def _load_environment(
    db: AsyncSession, environment_id: int | None
) -> tuple[dict | None, int | None]:
    """返回 (环境快照, 所属项目 id)；环境不存在时 404。"""
    if environment_id is None:
        return None, None
    env = await db.get(db_models.Environment, environment_id)
    if env is None:
        raise HTTPException(status_code=404, detail="环境不存在")
    # 031（US2）：secret 变量读取即解密 —— 调试发送绝不能把密文当值发出去
    from app.security.secret_vars import decrypt_variables_safe

    variables, decrypt_errors = decrypt_variables_safe(env.variables or [])
    if decrypt_errors:
        raise HTTPException(
            status_code=400,
            detail="无法解密变量 " + "、".join(decrypt_errors) + "：密文损坏或加密密钥已更换，请重新填写",
        )
    snapshot = {
        "id": env.id,
        "name": env.name,
        "base_url": env.base_url or "",
        "variables": variables,
        "headers": env.headers or [],
        # 031（US4）：多域名服务表
        "services": dict(getattr(env, "services", None) or {}),
    }
    return snapshot, env.project_id


@router.post("/debug")
async def debug_api_request(
    payload: dict,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> dict:
    """渲染（并可发送）单个接口请求，返回响应与断言/提取结果。"""
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="请求体必须是对象")
    step = payload.get("step")
    dry_run = bool(payload.get("dry_run"))
    if not isinstance(step, dict) or not isinstance(step.get("request"), dict):
        # 033（US5）：预览允许只带多步骤 api_spec（缺 step 时取首步作为锚点）
        spec_payload = payload.get("api_spec")
        spec_steps = spec_payload.get("steps") if isinstance(spec_payload, dict) else None
        if (
            dry_run
            and isinstance(spec_steps, list)
            and spec_steps
            and isinstance(spec_steps[0], dict)
        ):
            step = spec_steps[0]
        else:
            raise HTTPException(status_code=400, detail="step.request 缺失")
    environment, environment_project_id = await _load_environment(
        db, payload.get("environment_id")
    )
    if environment_project_id is not None:
        _ensure_project_access(user, environment_project_id)

    case_variables = payload.get("case_variables") or []
    # 031（US9）：调试单请求的数据驱动参数（覆盖用例绑定语义）
    # 注意：本端点 payload 是 **dict**（不是 Pydantic 模型）——属性访问会 500（实测踩过）
    spec = {"schema_version": 1, "fail_policy": "fail_fast", "steps": [step]}
    dataset_mode = payload.get("dataset_mode")
    if dataset_mode:
        spec["dataset_mode"] = dataset_mode
    loop_count = payload.get("loop_count")
    if loop_count is not None:
        spec["loop_count"] = loop_count
    fail_fast = payload.get("fail_fast")
    if fail_fast is not None:
        spec["fail_policy"] = "fail_fast" if fail_fast else "continue"

    if dry_run:
        # 033（US5）：优先使用完整 api_spec（编辑器发送时携带），支持多步骤预览
        preview_spec = payload.get("api_spec") if isinstance(payload.get("api_spec"), dict) else spec
        return await _render_only(preview_spec, environment, case_variables)

    from core.api_runner.runner import run_api_case

    # 031（US1）：解析 platform://<id> 测试文件引用（缺失时执行期给可读错误）
    try:
        from app.crud import api_file as crud_file

        refs = crud_file.iter_platform_refs(spec)
        platform_files = await crud_file.resolve_platform_paths(db, refs) if refs else {}
    except Exception:
        logger.warning("解析测试文件引用失败（按无文件调试）", exc_info=True)
        platform_files = {}

    result = await run_api_case(
        spec,
        environment=environment,
        case_variables=case_variables,
        platform_files=platform_files or None,
    )
    first = (result.steps or [{}])[0]
    if result.error and not result.steps:
        await _record_history(
            db, user, step, project_id=environment_project_id,
            status_code=None, duration_ms=result.duration_ms, error=result.error,
        )
        return {
            "rendered": {},
            "response": None,
            "assertions": [],
            "extracted": [],
            "error": result.error,
        }
    rendered = _rendered_payload(first.get("request") or {})
    response_payload = _response_payload(first.get("response"))

    # 031（US7 T029）：真实发送（非 dry_run）写请求历史；失败照录
    await _record_history(
        db,
        user,
        step,
        project_id=environment_project_id,
        status_code=(response_payload or {}).get("status"),
        duration_ms=(response_payload or {}).get("duration_ms"),
        headers_masked=rendered.get("headers") or {},
        body_preview=rendered.get("body_preview"),
        error=(first.get("error") if (response_payload or {}).get("status") is None else None),
    )
    return {
        "rendered": rendered,
        "response": response_payload,
        "assertions": first.get("assertions") or [],
        "extracted": _extracted_payload(first.get("extracted") or []),
        "error": first.get("error"),
    }


async def _record_history(
    db,
    user,
    step: dict,
    *,
    project_id,
    status_code=None,
    duration_ms=None,
    headers_masked=None,
    body_preview=None,
    error=None,
) -> None:
    """写历史；任何异常只记 warning（历史失败不得影响调试结果）。"""
    from app.crud import api_history as crud_hist

    request = (step or {}).get("request") or {}
    try:
        await crud_hist.add_history(
            db,
            user_id=getattr(user, "id", None),
            project_id=project_id,
            method=str(request.get("method") or "GET"),
            url=str(request.get("url") or ""),  # 模板态（含 {{变量}}，与 model 注释一致）
            headers_masked=headers_masked or {},
            body_preview=body_preview,
            status_code=status_code,
            duration_ms=duration_ms,
            error=error,
        )
    except Exception:  # noqa: BLE001 - 历史失败不得影响调试
        logger.warning("写调试请求历史失败", exc_info=True)


def _rendered_payload(request: dict) -> dict:
    """contracts §1.4：rendered = {method,url,headers,body_preview}，敏感头脱敏。"""
    from core.api_runner.variables import mask_headers

    body_preview, _ = _preview(_body_text(request.get("body")))
    return {
        "method": request.get("method"),
        "url": request.get("url"),
        "headers": mask_headers(request.get("headers") or {}),
        "body_preview": body_preview,
    }


def _body_text(body: Any) -> str:
    """请求体归一为字符串：None → 空串（GET 无体），非字符串 → JSON 文本。"""
    if body is None:
        return ""
    if isinstance(body, str):
        return body
    return json.dumps(body, ensure_ascii=False, default=str)


def _response_payload(response: Any) -> dict | None:
    """contracts §1.4：响应含 size/body_preview/truncated，响应头脱敏。"""
    if not isinstance(response, dict):
        return None
    from core.api_runner.variables import mask_headers

    body_text = str(response.get("body") or "")
    body_preview, truncated = _preview(body_text)
    return {
        "status": response.get("status"),
        "duration_ms": response.get("duration_ms"),
        "size": len(body_text.encode("utf-8")),
        "headers": mask_headers(response.get("headers") or {}),
        "body_preview": body_preview,
        "truncated": truncated,
    }


def _extracted_payload(extracted: list) -> list[dict]:
    """contracts §1.4：提取值只回传 variable + value_masked（密名变量必须打码）。"""
    from core.api_runner.variables import is_secret_name, mask_value

    out: list[dict] = []
    for item in extracted:
        if not isinstance(item, dict):
            continue
        variable = str(item.get("variable") or "")
        value = item.get("value")
        out.append(
            {
                "variable": variable,
                "value_masked": mask_value(value) if is_secret_name(variable) else ("" if value is None else str(value)),
            }
        )
    return out


def _walk_strings(value: Any, path: str):
    """递归收集 (路径, 字符串)（033 US5：unresolved 精确定位）。"""
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, dict):
        for k, v in value.items():
            yield from _walk_strings(v, f"{path}.{k}" if path else str(k))
    elif isinstance(value, list):
        for i, v in enumerate(value):
            yield from _walk_strings(v, f"{path}[{i}]")


def _scope_of(name: str, scope) -> str:
    """变量来源分类（与 lookup 优先级一致）。"""
    if name.startswith("$"):
        return "builtin"
    for layer, label in (
        (scope.runtime, "runtime"),
        (scope.step, "step"),
        (scope.case, "case"),
        (scope.dataset, "dataset"),
        (scope.services, "services"),
        (scope.env, "environment"),
    ):
        if name in layer:
            return label
    if name in ("baseUrl", "base_url", "BASE_URL") and scope.base_url:
        return "baseUrl"
    return "unknown"


def _auth_plan(spec: dict, environment: dict) -> dict:
    """033（US5）：预览将使用的认证与来源（不含 secret）。

    解析顺序与执行一致：步骤 → 用例（api_spec.auth）→ 环境默认；inherit 逐层下落。
    """
    steps = spec.get("steps") or []
    step_auth = None
    if steps and isinstance(steps[0], dict):
        step_auth = ((steps[0].get("request") or {}).get("auth")) or None
    case_auth = spec.get("auth") if isinstance(spec.get("auth"), dict) else None
    env_auth = (environment or {}).get("default_auth") if isinstance(environment, dict) else None

    for layer, source in ((step_auth, "step"), (case_auth, "case"), (env_auth, "environment")):
        if not isinstance(layer, dict):
            continue
        if str(layer.get("type") or "inherit") == "inherit":
            continue
        plan: dict = {"type": str(layer.get("type") or "none"), "source": source}
        if plan["type"] == "oauth2":
            plan["token_var"] = str((layer.get("oauth2") or {}).get("token_var") or "access_token")
        return plan
    return {"type": "none", "source": "none"}


async def _render_only(
    spec: dict, environment: dict | None, case_variables: list[dict]
) -> dict:
    """dry_run（033 US5 扩展）：渲染**全部步骤** + 变量解析轨迹 + unresolved + auth_plan。

    零出站；``rendered`` 保持第一步对象（向后兼容），新增 ``rendered_steps`` 全量数组。
    """
    from core.api_runner.request_builder import apply_auth_chain, build_request
    from core.api_runner.variables import (
        UndefinedVariableError,
        VariableError,
        VariableScope,
        _builtin as _builtin_value,
        _VAR_RE,
        is_secret_name,
        secret_keys,
        variables_to_map,
    )

    env = environment or {}
    raw_spec = spec if isinstance(spec, dict) else {}
    # auth_plan 必须在链解析前计算（保留"来源层"信息）
    auth_plan = _auth_plan(raw_spec, env)
    spec = apply_auth_chain(raw_spec, env_default_auth=env.get("default_auth"))
    # 用例级变量：spec.variables 打底，显式 case_variables 覆盖（与执行器口径一致）
    case_map = variables_to_map(spec.get("variables"))
    case_map.update(variables_to_map(case_variables))
    scope = VariableScope(
        case_variables=case_map,
        # 031（US4）：多域名服务名同样参与渲染与「可用变量」提示
        services=dict(env.get("services") or {}),
        env_variables=variables_to_map(env.get("variables")),
        base_url=env.get("base_url") or "",
    )
    secret_names = secret_keys(spec.get("variables")) | secret_keys(env.get("variables"))

    steps = [s for s in (spec.get("steps") or []) if isinstance(s, dict)]
    if not steps:
        return {
            "rendered": {},
            "rendered_steps": [],
            "variables_trace": [],
            "unresolved": [],
            "auth_plan": auth_plan,
            "response": None,
            "assertions": [],
            "extracted": [],
            "error": "没有可预览的步骤",
        }

    unresolved: list[dict] = []
    seen_unresolved: set = set()
    trace: dict[str, dict] = {}
    rendered_steps: list[dict] = []
    first_rendered: dict | None = None
    first_error: str | None = None

    for idx, st in enumerate(steps):
        scope.step = variables_to_map(st.get("variables"))
        # ① 扫描：unresolved 精确定位 + 变量轨迹
        for path, text in _walk_strings(st, ""):
            if "{{" not in text:
                continue
            for m in _VAR_RE.finditer(text):
                name = m.group(1).strip()
                if not name:
                    continue
                builtin_value = None
                if name.startswith("$"):
                    builtin_value = _builtin_value(name)
                value = builtin_value if builtin_value is not None else scope.lookup(name)
                if name not in trace:
                    is_secret = bool(name in secret_names or is_secret_name(name))
                    trace[name] = {
                        "name": name,
                        "scope": _scope_of(name, scope),
                        "value": "******" if is_secret else ("" if value is None else str(value)),
                        "secret": is_secret,
                    }
                if value is None:
                    key = (name, idx, path)
                    if key not in seen_unresolved:
                        seen_unresolved.add(key)
                        unresolved.append({"name": name, "step_index": idx, "field": path})
        # ② oauth2 步骤：预览用占位令牌（不触发真实取令牌）
        auth = (st.get("request") or {}).get("auth") or {}
        if str(auth.get("type") or "") == "oauth2":
            token_var = str((auth.get("oauth2") or {}).get("token_var") or "access_token")
            scope.set_runtime(token_var, "（运行期获取）")
        # ③ 渲染
        try:
            rendered = build_request(st, scope)
            item = {
                "step_index": idx,
                **_rendered_payload(
                    {
                        "method": rendered.method,
                        "url": rendered.url,
                        "headers": rendered.headers,
                        "body": rendered.content,
                    }
                ),
            }
            rendered_steps.append(item)
            if first_rendered is None:
                first_rendered = item
        except UndefinedVariableError as exc:
            rendered_steps.append({"step_index": idx, "error": str(exc)})
            first_error = first_error or str(exc)
        except VariableError as exc:
            rendered_steps.append({"step_index": idx, "error": str(exc)})
            first_error = first_error or str(exc)
        except Exception as exc:  # noqa: BLE001 - 预览绝不 500
            logger.warning("接口调试 dry_run 渲染失败: %s", exc, exc_info=True)
            rendered_steps.append({"step_index": idx, "error": f"请求渲染失败: {exc}"})
            first_error = first_error or f"请求渲染失败: {exc}"

    # 轨迹里可能出现步骤内新增变量：以最终扫描为准（保序）
    trace_list = list(trace.values())
    return {
        "rendered": {k: v for k, v in (first_rendered or {}).items() if k != "step_index"},
        "rendered_steps": rendered_steps,
        "variables_trace": trace_list,
        "unresolved": unresolved,
        "auth_plan": auth_plan,
        "response": None,
        "assertions": [],
        "extracted": [],
        "error": first_error,
    }


@router.get("/history")
async def list_request_history(
    project_id: Optional[int] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    before_id: Optional[int] = Query(default=None),
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> dict:
    """调试请求历史（倒序；按用户隔离，contracts §1.2）。"""
    from app.crud import api_history as crud_hist

    items = await crud_hist.list_history(
        db, user_id=user.id, project_id=project_id, limit=limit, before_id=before_id
    )
    return {"total": len(items), "items": items}


@router.delete("/history")
async def clear_request_history(
    project_id: Optional[int] = Query(default=None),
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> dict:
    """清空当前用户历史（可按项目限定；contracts §1.3）。"""
    from app.crud import api_history as crud_hist

    removed = await crud_hist.clear_history(db, user_id=user.id, project_id=project_id)
    return {"removed": removed}


class _ScriptTryRequest(BaseModel):
    """031（US3）：脚本试跑请求（零出站 —— 只跑沙箱，不发任何 HTTP）。"""

    script: str = ""
    phase: str = "pre"  # pre | post
    variables: dict[str, Any] = Field(default_factory=dict)
    response: Optional[dict[str, Any]] = None
    timeout_ms: Optional[int] = None


@router.post("/debug/script")
async def debug_script(
    payload: _ScriptTryRequest,
    _user: db_models.User = Depends(get_current_user),  # noqa: B008
) -> dict[str, Any]:
    """试跑前后置脚本：返回 {ok, variables, output, error}（contracts §5；零出站）。"""
    from core.script_sandbox import run_script

    context: dict[str, Any] = {"vars": dict(payload.variables or {})}
    if payload.phase == "post" and payload.response:
        resp = payload.response
        context.update(
            {
                "status_code": resp.get("status_code", 0),
                "duration_ms": resp.get("duration_ms", 0),
                "headers": {str(k).lower(): v for k, v in (resp.get("headers") or {}).items()},
                "body": resp.get("body"),
                "body_text": resp.get("body_text") or "",
            }
        )
    timeout = payload.timeout_ms if isinstance(payload.timeout_ms, int) else None
    if timeout is not None and not 200 <= timeout <= 10000:
        timeout = None
    if timeout is None:
        from app.config import get_settings

        timeout = int(get_settings().api_script_timeout_ms)
    out = run_script(
        payload.script,
        context,
        timeout_ms=timeout,
        with_checks=True,
        with_request_overrides=True,
        with_cookies=True,
    )
    return {
        "ok": out.ok,
        "variables": out.variables,
        "output": out.output,
        "error": out.error,
        # 033（US4）：命名断言试跑结果 [{name, ok, message}]
        "tests": out.tests,
        # 033（US7）：请求改写试跑结果（结构化覆盖记录）
        "request_overrides": out.request_overrides,
        # 033（US8）：cookie 读写试跑结果
        "set_cookies": out.set_cookies,
        "delete_cookies": out.delete_cookies,
    }


class _SuggestAssertionsRequest(BaseModel):
    """031（US11）：从响应样本生成候选断言。"""

    method: str = "GET"
    url: str = ""
    status_code: Optional[int] = None
    headers: dict[str, Any] = Field(default_factory=dict)
    body_text: str = ""


@router.post("/debug/suggest-assertions")
async def suggest_assertions_endpoint(
    payload: _SuggestAssertionsRequest,
    _user: db_models.User = Depends(get_current_user),  # noqa: B008
) -> dict[str, Any]:
    """AI 断言建议：LLM → 服务端 schema 校验 → 合法候选（非法候选附原因丢弃）。

    LLM 不可用/输出不可解析 → **503**（前端据此降级隐藏入口）。
    """
    from app.services.api_ai import ApiAiUnavailable, suggest_assertions

    try:
        return await suggest_assertions(payload.model_dump())
    except ApiAiUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
