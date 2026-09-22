# app/routers/api_test/debug.py — 接口调试发送（029-api-testing 契约 §1.4）
#
# Postman 的「Send」：**不写库**（不建 test_runs / run_logs），只渲染并可选发送。
# dry_run=true 时只做变量渲染与校验，用于发送前的缺失变量提示。
from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
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
    snapshot = {
        "id": env.id,
        "name": env.name,
        "base_url": env.base_url or "",
        "variables": env.variables or [],
        "headers": env.headers or [],
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
    spec = {"schema_version": 1, "fail_policy": "fail_fast", "steps": [step]}

    if dry_run:
        return await _render_only(spec, environment, case_variables)

    from core.api_runner.runner import run_api_case

    result = await run_api_case(
        spec, environment=environment, case_variables=case_variables
    )
    first = (result.steps or [{}])[0]
    if result.error and not result.steps:
        return {
            "rendered": {},
            "response": None,
            "assertions": [],
            "extracted": [],
            "error": result.error,
        }
    return {
        "rendered": _rendered_payload(first.get("request") or {}),
        "response": _response_payload(first.get("response")),
        "assertions": first.get("assertions") or [],
        "extracted": _extracted_payload(first.get("extracted") or []),
        "error": first.get("error"),
    }


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
