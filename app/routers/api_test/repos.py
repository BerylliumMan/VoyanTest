"""046：代码仓库绑定 / 连通性测试 / 手动同步。"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Literal
from urllib.parse import unquote, urlsplit, urlunsplit

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user, get_user_project_filter
from app.config import get_settings
from app.database import get_async_db
from app.models.api_extract import ApiSyncRecord, CodeRepoBinding
from app.security.encryption import encrypt_value
from app.services.api_change_plans import spawn_background
from app.services.api_sync import binding_auth, repo_workdir, sync_binding
from core.git_sync import GitSyncError, ls_remote, mask_url

logger = logging.getLogger(__name__)

router = APIRouter()
MASK = "******"
AuthType = Literal["https_token", "ssh_key"]
SyncMode = Literal["manual", "scheduled"]
ExtractorName = Literal["auto", "openapi", "spring_source"]


class RepoBindingIn(BaseModel):
    project_id: int
    repo_url: str = Field(min_length=1, max_length=500)
    branch: str = Field(default="main", max_length=200)
    auth_type: AuthType = "https_token"
    auth_secret: str | None = None
    sync_mode: SyncMode = "scheduled"
    sync_interval_minutes: int | None = Field(default=None, ge=5, le=1440)
    extractor: ExtractorName = "auto"
    openapi_url: str | None = Field(default=None, max_length=500)
    source_root: str | None = Field(default=None, max_length=300)
    confirm_window_minutes: int | None = Field(default=None, ge=5, le=1440)
    full_sync_every: int | None = Field(default=None, ge=1, le=1000)


class RepoBindingPatch(BaseModel):
    repo_url: str | None = Field(default=None, min_length=1, max_length=500)
    branch: str | None = Field(default=None, max_length=200)
    auth_type: AuthType | None = None
    auth_secret: str | None = None
    sync_mode: SyncMode | None = None
    sync_interval_minutes: int | None = Field(default=None, ge=5, le=1440)
    extractor: ExtractorName | None = None
    openapi_url: str | None = Field(default=None, max_length=500)
    source_root: str | None = Field(default=None, max_length=300)
    confirm_window_minutes: int | None = Field(default=None, ge=5, le=1440)
    full_sync_every: int | None = Field(default=None, ge=1, le=1000)


def _ensure_project_access(user: Any, project_id: int) -> None:
    allowed = get_user_project_filter(user)
    if allowed is not None and project_id not in allowed:
        raise HTTPException(status_code=403, detail="无权访问该项目")


def _clean_source_root(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip().strip("/")
    if not value:
        return None
    if ".." in value.split("/"):
        raise HTTPException(status_code=400, detail="源码目录不能包含 ..")
    return value


def _split_userinfo(repo_url: str) -> tuple[str, str | None]:
    """把 URL 里的 user:pass 拆出来，避免凭据留在明文地址里。"""
    parts = urlsplit(repo_url)
    if not parts.username and not parts.password:
        return repo_url, None
    user = unquote(parts.username or "")
    pwd = unquote(parts.password or "")
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    clean = urlunsplit((parts.scheme, host, parts.path, parts.query, parts.fragment))
    secret = f"{user}:{pwd}" if user else pwd
    return clean, secret or None


def _serialize(binding: CodeRepoBinding) -> dict:
    return {
        "id": binding.id,
        "project_id": binding.project_id,
        "repo_url": mask_url(binding.repo_url),
        "branch": binding.branch,
        "auth_type": binding.auth_type,
        "has_secret": bool(binding.auth_secret),
        "auth_secret": MASK if binding.auth_secret else None,
        "sync_mode": binding.sync_mode,
        "sync_interval_minutes": binding.sync_interval_minutes,
        "extractor": binding.extractor,
        "openapi_url": binding.openapi_url,
        "source_root": binding.source_root,
        "confirm_window_minutes": binding.confirm_window_minutes,
        "full_sync_every": binding.full_sync_every,
        "last_sync_at": binding.last_sync_at.isoformat() if binding.last_sync_at else None,
        "last_commit": binding.last_commit,
        "last_error": binding.last_error,
        "created_at": binding.created_at.isoformat() if binding.created_at else None,
        "updated_at": binding.updated_at.isoformat() if binding.updated_at else None,
    }


def _serialize_record(record: ApiSyncRecord) -> dict:
    return {
        "id": record.id,
        "trigger": record.trigger,
        "status": record.status,
        "started_at": record.started_at.isoformat() if record.started_at else None,
        "finished_at": record.finished_at.isoformat() if record.finished_at else None,
        "base_commit": record.base_commit,
        "head_commit": record.head_commit,
        "added": record.added,
        "updated": record.updated,
        "removed": record.removed,
        "error": record.error,
        "details": record.details or {},
    }


async def _load_binding(db: AsyncSession, project_id: int) -> CodeRepoBinding | None:
    return (
        await db.execute(
            select(CodeRepoBinding).where(CodeRepoBinding.project_id == project_id)
        )
    ).scalar_one_or_none()


def _apply_values(binding: CodeRepoBinding, data: dict) -> None:
    repo_url = data.pop("repo_url", None)
    if repo_url:
        clean, secret = _split_userinfo(repo_url)
        binding.repo_url = clean
        if secret:
            binding.auth_secret = encrypt_value(secret)
    secret = data.pop("auth_secret", None)
    if secret is not None and secret != MASK:
        binding.auth_secret = encrypt_value(secret) if secret else None
    # 可清空字段：显式赋值（空串/None 表示清空）
    if "source_root" in data:
        binding.source_root = _clean_source_root(data.pop("source_root"))
    if "openapi_url" in data:
        binding.openapi_url = data.pop("openapi_url") or None
    for key, value in data.items():
        if value is not None:
            setattr(binding, key, value)


@router.get("/repos/{project_id}")
async def get_repo_binding(
    project_id: int,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> dict:
    _ensure_project_access(user, project_id)
    binding = await _load_binding(db, project_id)
    if binding is None:
        return {"bound": False, "binding": None}
    return {"bound": True, "binding": _serialize(binding)}


@router.post("/repos", status_code=201)
async def bind_repo(
    payload: RepoBindingIn,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> dict:
    _ensure_project_access(user, payload.project_id)
    data = payload.model_dump()
    project_id = data.pop("project_id")

    binding = await _load_binding(db, project_id)
    created = binding is None
    if binding is None:
        binding = CodeRepoBinding(project_id=project_id, repo_url=payload.repo_url)
        db.add(binding)

    if data.get("sync_interval_minutes") is None:
        data["sync_interval_minutes"] = get_settings().api_extract_sync_interval_minutes
    _apply_values(binding, data)
    if not binding.repo_url:
        raise HTTPException(status_code=400, detail="仓库地址不能为空")
    await db.commit()
    await db.refresh(binding)
    return {"created": created, "binding": _serialize(binding)}


@router.put("/repos/{project_id}")
async def update_repo_binding(
    project_id: int,
    payload: RepoBindingPatch,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> dict:
    _ensure_project_access(user, project_id)
    binding = await _load_binding(db, project_id)
    if binding is None:
        raise HTTPException(status_code=404, detail="尚未绑定仓库")
    _apply_values(binding, payload.model_dump(exclude_unset=True))
    await db.commit()
    await db.refresh(binding)
    return {"binding": _serialize(binding)}


@router.delete("/repos/{project_id}")
async def unbind_repo(
    project_id: int,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> dict:
    _ensure_project_access(user, project_id)
    binding = await _load_binding(db, project_id)
    if binding is None:
        raise HTTPException(status_code=404, detail="尚未绑定仓库")
    await db.delete(binding)
    await db.commit()
    return {"deleted": project_id}


@router.post("/repos/{project_id}/test")
async def test_repo_connection(
    project_id: int,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> dict:
    """连通性测试：只列远端引用，不克隆。"""
    _ensure_project_access(user, project_id)
    binding = await _load_binding(db, project_id)
    if binding is None:
        raise HTTPException(status_code=404, detail="尚未绑定仓库")
    repo_url = binding.repo_url
    try:
        auth = binding_auth(binding)
    except GitSyncError as exc:
        return {"ok": False, "error": str(exc)}

    try:
        refs = await asyncio.to_thread(
            ls_remote, repo_url, auth=auth, timeout=get_settings().api_repo_clone_timeout_seconds
        )
    except GitSyncError as exc:
        return {"ok": False, "error": str(exc)}

    branches = sorted(
        ref[len("refs/heads/") :] for ref in refs if ref.startswith("refs/heads/")
    )
    head = refs.get("HEAD") or (refs.get(f"refs/heads/{binding.branch}") if binding.branch else None)
    return {
        "ok": True,
        "head": head,
        "branches": branches[:50],
        "branch_exists": (not binding.branch) or binding.branch in branches,
    }


@router.post("/repos/{project_id}/sync")
async def trigger_sync(
    project_id: int,
    wait: bool = False,
    check: bool = False,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> dict:
    """手动同步。默认后台执行，结果通过同步记录查询。"""
    _ensure_project_access(user, project_id)
    binding = await _load_binding(db, project_id)
    if binding is None:
        raise HTTPException(status_code=404, detail="尚未绑定仓库")
    binding_id = binding.id

    async def _run() -> None:
        try:
            from app.database import AsyncSessionLocal

            async with AsyncSessionLocal() as session:
                fresh = await session.get(CodeRepoBinding, binding_id)
                if fresh is not None:
                    if check:
                        from app.services.api_change_plans import sync_and_plan_once

                        await sync_and_plan_once(binding_id, trigger="manual")
                    else:
                        await sync_binding(session, fresh, trigger="manual")
        except Exception:  # noqa: BLE001 - 后台任务失败已写入同步记录
            logger.warning("手动同步失败 project=%s", project_id, exc_info=True)

    if wait:
        await _run()
        return {"status": "done"}

    spawn_background(_run())
    return {"status": "started", "workdir": repo_workdir(project_id), "check": check}


@router.post("/repos/{project_id}/sync/cancel")
async def cancel_sync(
    project_id: int,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> dict:
    """请求取消该项目的同步（协作式：文件粒度中断）。"""
    _ensure_project_access(user, project_id)
    binding = await _load_binding(db, project_id)
    if binding is None:
        raise HTTPException(status_code=404, detail="尚未绑定仓库")
    from sqlalchemy import update

    from app.services.api_sync import request_cancel

    result = await db.execute(
        update(ApiSyncRecord)
        .where(
            ApiSyncRecord.project_id == project_id,
            ApiSyncRecord.status == "running",
        )
        .values(cancel_requested=True)
        .execution_options(synchronize_session=False)
    )
    await db.commit()
    memory_hit = request_cancel(project_id)
    return {
        "requested": bool(result.rowcount),
        "memory_hit": memory_hit,
        "project_id": project_id,
    }


@router.get("/repos/{project_id}/syncs")
async def list_sync_records(
    project_id: int,
    limit: int = 20,
    status: str | None = None,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> dict:
    _ensure_project_access(user, project_id)
    limit = max(1, min(limit, 100))
    stmt = select(ApiSyncRecord).where(ApiSyncRecord.project_id == project_id)
    if status:
        stmt = stmt.where(ApiSyncRecord.status == status)
    rows = (
        await db.execute(stmt.order_by(ApiSyncRecord.id.desc()).limit(limit))
    ).scalars().all()
    return {"items": [_serialize_record(r) for r in rows]}
