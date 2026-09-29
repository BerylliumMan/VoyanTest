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
    if not isinstance(step, dict) or not isinstance(step.get("request"), dict):
        raise HTTPException(status_code=400, detail="step.request 缺失")
    environment, environment_project_id = await _load_environment(
        db, payload.get("environment_id")
    )
    if environment_project_id is not None:
        _ensure_project_access(user, environment_project_id)

    case_variables = payload.get("case_variables") or []
    dry_run = bool(payload.get("dry_run"))
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
        return await _render_only(spec, environment, case_variables)

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


async def _render_only(
    spec: dict, environment: dict | None, case_variables: list[dict]
) -> dict:
    """dry_run：只渲染请求，验证变量是否齐全（不发网络请求）。"""
    from core.api_runner.request_builder import build_request
    from core.api_runner.variables import (
        UndefinedVariableError,
        VariableError,
        VariableScope,
        variables_to_map,
    )

    env = environment or {}
    scope = VariableScope(
        case_variables=variables_to_map(case_variables),
        # 031（US4）：多域名服务名同样参与渲染与「可用变量」提示
        services=dict(env.get("services") or {}),
        env_variables=variables_to_map(env.get("variables")),
        base_url=env.get("base_url") or "",
    )
    try:
        rendered = build_request(spec["steps"][0], scope)
    except UndefinedVariableError as exc:
        return {
            "rendered": {},
            "response": None,
            "assertions": [],
            "extracted": [],
            "error": str(exc),
        }
    except VariableError as exc:
        return {
            "rendered": {},
            "response": None,
            "assertions": [],
            "extracted": [],
            "error": str(exc),
        }
    except Exception as exc:
        logger.warning("接口调试 dry_run 渲染失败: %s", exc, exc_info=True)
        return {
            "rendered": {},
            "response": None,
            "assertions": [],
            "extracted": [],
            "error": f"请求渲染失败: {exc}",
        }
    return {
        "rendered": _rendered_payload(
            {
                "method": rendered.method,
                "url": rendered.url,
                "headers": rendered.headers,
                "body": rendered.content,
            }
        ),
        "response": None,
        "assertions": [],
        "extracted": [],
        "error": None,
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
    out = run_script(payload.script, context, timeout_ms=timeout)
    return {
        "ok": out.ok,
        "variables": out.variables,
        "output": out.output,
        "error": out.error,
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
