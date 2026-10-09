"""033（US1）：接口用例体检端点（Inspections）。

``POST /api/api-test/inspect`` —— 纯静态、零出站；不阻断保存/执行。
契约见 ``specs/033-api-test-maturity/contracts/api-maturity.md`` §C1。
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app import db_models
from app.auth import get_current_user, get_user_project_filter
from app.config import get_settings
from app.database import get_async_db

logger = logging.getLogger(__name__)

router = APIRouter()


class InspectRequest(BaseModel):
    case_id: Optional[int] = None
    spec: Optional[dict] = None
    environment_id: Optional[int] = None
    dataset_id: Optional[int] = None


def _ensure_project_access(user: Any, project_id: int) -> None:
    allowed = get_user_project_filter(user)
    if allowed is not None and project_id not in allowed:
        raise HTTPException(status_code=403, detail="无权访问该项目")


async def _load_environment(db: AsyncSession, environment_id: int) -> tuple[dict, int]:
    """返回（环境快照, 所属项目 id）；环境不存在时 404。"""
    env = await db.get(db_models.Environment, environment_id)
    if env is None:
        raise HTTPException(status_code=404, detail="环境不存在")
    from app.security.secret_vars import decrypt_variables_safe

    variables, decrypt_errors = decrypt_variables_safe(env.variables or [])
    if decrypt_errors:
        raise HTTPException(
            status_code=400,
            detail="无法解密变量 " + "、".join(decrypt_errors) + "：密文损坏或加密密钥已更换，请重新填写",
        )
    snapshot = {
        "base_url": env.base_url or "",
        "variables": variables,
        "services": dict(getattr(env, "services", None) or {}),
    }
    return snapshot, env.project_id


@router.post("/inspect")
async def inspect_case(
    payload: InspectRequest,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> dict:
    """对接口用例（case_id）或原始 api_spec 执行静态体检。"""
    settings = get_settings()
    if not settings.api_inspections_enabled:
        return {"findings": [], "disabled": True}

    spec: Optional[dict] = None
    dataset_id: Optional[int] = payload.dataset_id

    if payload.case_id is not None:
        case = await db.get(db_models.TestCase, payload.case_id)
        if case is None:
            raise HTTPException(status_code=404, detail="用例不存在")
        _ensure_project_access(user, case.project_id)
        spec = getattr(case, "api_spec", None)
        if not isinstance(spec, dict) or not spec:
            raise HTTPException(status_code=400, detail="该用例不是接口用例（无 api_spec）")
        if dataset_id is None and spec.get("dataset_id"):
            dataset_id = int(spec["dataset_id"])
    elif isinstance(payload.spec, dict):
        spec = payload.spec
        if dataset_id is None and spec.get("dataset_id"):
            dataset_id = int(spec["dataset_id"])
    else:
        raise HTTPException(status_code=400, detail="需要提供 case_id 或 spec")

    environment = None
    if payload.environment_id is not None:
        environment, env_project_id = await _load_environment(db, payload.environment_id)
        _ensure_project_access(user, env_project_id)

    dataset = None
    if dataset_id is not None:
        row = await db.get(db_models.ApiDataset, dataset_id)
        if row is not None:
            _ensure_project_access(user, row.project_id)
            dataset = {"columns": row.columns or [], "rows": row.rows or []}
        # 数据集不存在时保持 None —— 由体检规则产出 dataset_missing 结论

    from core.api_inspections import inspect_api_spec

    try:
        findings = inspect_api_spec(spec, environment=environment, dataset=dataset)
    except Exception:  # 体检异常绝不阻塞主链路（降级为可用性提示）
        logger.warning("接口体检执行异常（降级为空结论）", exc_info=True)
        return {"findings": [], "disabled": False}

    return {"findings": [f.to_dict() for f in findings], "disabled": False}
