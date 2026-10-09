"""033（US11）：代码片段生成端点（服务端渲染 + 打码，零出站）。

``POST /api/api-test/snippets`` —— 给用例或原始 spec 的第 N 步生成
cURL / Python / JS 片段；渲染使用与执行一致的变量作用域，secret 值一律打码。
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app import db_models
from app.auth import get_current_user, get_user_project_filter
from app.database import get_async_db

logger = logging.getLogger(__name__)

router = APIRouter()


class SnippetRequest(BaseModel):
    case_id: Optional[int] = None
    spec: Optional[dict] = None
    step_index: int = 0
    environment_id: Optional[int] = None


def _ensure_project_access(user: Any, project_id: int) -> None:
    allowed = get_user_project_filter(user)
    if allowed is not None and project_id not in allowed:
        raise HTTPException(status_code=403, detail="无权访问该项目")


async def _env_snapshot(db: AsyncSession, environment_id: int) -> tuple[dict, int]:
    env = await db.get(db_models.Environment, environment_id)
    if env is None:
        raise HTTPException(status_code=404, detail="环境不存在")
    from app.security.secret_vars import decrypt_variables_safe

    variables, errors = decrypt_variables_safe(env.variables or [])
    if errors:
        raise HTTPException(
            status_code=400,
            detail="无法解密变量 " + "、".join(errors) + "：密文损坏或加密密钥已更换，请重新填写",
        )
    return (
        {
            "base_url": env.base_url or "",
            "variables": variables,
            "services": dict(getattr(env, "services", None) or {}),
            "default_auth": (
                env.default_auth if isinstance(getattr(env, "default_auth", None), dict) else None
            ),
        },
        env.project_id,
    )


@router.post("/snippets")
async def build_case_snippets(
    payload: SnippetRequest,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> dict:
    """生成三种语言的代码片段（渲染 + 打码；输入不落库）。"""
    spec: Optional[dict] = None
    if payload.case_id is not None:
        case = await db.get(db_models.TestCase, payload.case_id)
        if case is None:
            raise HTTPException(status_code=404, detail="用例不存在")
        _ensure_project_access(user, case.project_id)
        spec = getattr(case, "api_spec", None)
        if not isinstance(spec, dict) or not spec:
            raise HTTPException(status_code=400, detail="该用例不是接口用例（无 api_spec）")
    elif isinstance(payload.spec, dict):
        spec = payload.spec
    else:
        raise HTTPException(status_code=400, detail="需要提供 case_id 或 spec")

    environment: dict = {}
    if payload.environment_id is not None:
        environment, project_id = await _env_snapshot(db, payload.environment_id)
        _ensure_project_access(user, project_id)

    from core.api_runner.request_builder import apply_auth_chain, build_request
    from core.api_runner.variables import (
        VariableScope,
        mask_headers,
        mask_secrets_in_text,
        secret_keys,
        variables_to_map,
    )
    from core.snippets import build_snippets

    steps = [s for s in (spec.get("steps") or []) if isinstance(s, dict)]
    index = payload.step_index or 0
    if index < 0 or index >= len(steps):
        raise HTTPException(status_code=400, detail=f"step_index 越界（0..{max(0, len(steps) - 1)}）")

    resolved_spec = apply_auth_chain(spec, env_default_auth=environment.get("default_auth"))
    step = resolved_spec["steps"][index]

    case_map = variables_to_map(spec.get("variables"))
    scope = VariableScope(
        case_variables=case_map,
        services=dict(environment.get("services") or {}),
        env_variables=variables_to_map(environment.get("variables")),
        base_url=environment.get("base_url") or "",
    )
    try:
        rendered = build_request(step, scope)
    except Exception as exc:  # noqa: BLE001 - 渲染失败转 400（含未定义变量）
        raise HTTPException(status_code=400, detail=f"请求渲染失败: {exc}") from exc

    # secret 值集合（渲染后实际值；含用例与环境 secret 变量）
    secret_names = secret_keys(spec.get("variables")) | secret_keys(environment.get("variables"))
    secret_values = [
        str(scope.lookup(name))
        for name in secret_names
        if scope.lookup(name) not in (None, "")
    ]

    body_text = rendered.content or ""
    masked_url = mask_secrets_in_text(rendered.url, secret_values)
    masked_body = mask_secrets_in_text(body_text, secret_values) if body_text else None
    masked_headers = mask_headers(rendered.headers)
    # 头值里可能内嵌 secret 变量值（如 X-T: Bearer {{token}}）→ 逐值再打码
    masked_headers = {
        str(k): mask_secrets_in_text(v, secret_values)
        for k, v in (masked_headers or {}).items()
    }

    snippets = build_snippets(
        method=rendered.method,
        url=masked_url,
        headers={str(k): str(v) for k, v in masked_headers.items()},
        body_text=masked_body,
    )
    return {
        "snippets": snippets,
        "rendered": {
            "method": rendered.method,
            "url": masked_url,
            "header_count": len(masked_headers or {}),
        },
        "masked": bool(secret_values),
    }
