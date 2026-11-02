"""046：仓库同步服务。

- `apply_extracted_apis`：把提取结果合并进 api_definitions（幂等、保护人工修正）
- `sync_binding`：拉取/更新仓库 → 探测技术栈 → 提取 → 应用 → 记录
"""

from __future__ import annotations

import json
import logging
import shutil
import threading
from asyncio import to_thread
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.api_extract import ApiSyncRecord, CodeRepoBinding
from app.models.api_test import ApiDefinition
from app.security.encryption import decrypt_value
from app.tz import now as tz_now
from core.api_extract.base import ExtractContext, ExtractedApi, ExtractorError
from core.api_extract.detect import TechStack, detect_tech_stack
from core.api_extract.registry import resolve_extractors
from core.git_sync import (
    GitSyncError,
    RepoAuth,
    clone_repository,
    head_commit,
    list_changed_files,
    update_repository,
)

logger = logging.getLogger(__name__)

_AUTO_EXTRACTORS = {"openapi", "spring_source"}
REPO_ROOT = Path("data/api_repos")


@dataclass
class ApplyStats:
    added: int = 0
    updated: int = 0
    removed: int = 0
    skipped_manual: int = 0
    unchanged: int = 0

    def as_dict(self) -> dict:
        return {
            "added": self.added,
            "updated": self.updated,
            "removed": self.removed,
            "skipped_manual": self.skipped_manual,
            "unchanged": self.unchanged,
        }


@dataclass
class SyncOutcome:
    status: str = "success"  # success | failed | no_change | disabled | cancelled
    base_commit: str | None = None
    head_commit: str | None = None
    stats: ApplyStats = field(default_factory=ApplyStats)
    tech: TechStack | None = None
    extractors: list[str] = field(default_factory=list)
    error: str | None = None
    # 046 v2
    changed_files: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    incremental: bool = False


# 046 v2：协作式取消（进程内事件；跨 worker 时以 DB 标志在阶段边界生效）
_CANCEL_FLAGS: dict[int, threading.Event] = {}


def request_cancel(project_id: int) -> bool:
    """请求取消该项目的同步；返回是否命中运行中的同步。"""
    event = _CANCEL_FLAGS.get(int(project_id))
    if event is None:
        return False
    event.set()
    return True


def repo_workdir(project_id: int) -> str:
    """仓库本地工作目录（data 卷持久化）。"""
    return str(REPO_ROOT / str(project_id))


def _display_name(api: ExtractedApi) -> str:
    return (api.summary or "").strip() or api.source_symbol or f"{api.method} {api.path}"


def _source_label(extractor: str) -> str:
    return {"openapi": "openapi3", "spring_source": "spring"}.get(extractor, extractor or "manual")


def _json_eq(a, b) -> bool:
    def _dump(v):
        return json.dumps(v if v is not None else {}, sort_keys=True, ensure_ascii=False, default=str)

    return _dump(a) == _dump(b)


async def apply_extracted_apis(
    db: AsyncSession,
    *,
    project_id: int,
    apis: list[ExtractedApi],
    extractor: str,
    module_id: int | None = None,
    offline_extractors: set[str] | None = None,
    allow_offline: bool = True,
) -> ApplyStats:
    """把一次提取结果合并进接口定义。

    - (method, path) 为唯一键；新增插入、已存在更新
    - `manual_override=True` 的定义不被覆盖，也不被下线
    - 本次未出现的自动提取定义标记 offline（保留用例引用，不物理删除）
    """
    rows = (
        await db.execute(select(ApiDefinition).where(ApiDefinition.project_id == project_id))
    ).scalars().all()
    existing: dict[tuple[str, str], ApiDefinition] = {
        (r.method.upper(), r.path): r for r in rows
    }

    stats = ApplyStats()
    seen: set[tuple[str, str]] = set()
    for api in apis:
        key = (api.method, api.path)
        if key in seen:
            continue
        seen.add(key)

        # 每条接口保留自己的提取器（auto 模式下可能同时有 openapi 与 spring_source）
        api_extractor = (api.extractor or extractor)
        obj = existing.get(key)
        if obj is None:
            obj = ApiDefinition(
                project_id=project_id,
                module_id=module_id,
                name=_display_name(api),
                method=api.method,
                path=api.path,
                protocol="http",
                summary=api.summary or None,
                operation_id=api.operation_id,
                request_schema=api.request_schema or {},
                response_schema=api.response_schema,
                tags=api.tags,
                source=_source_label(api_extractor),
                source_file=api.source_file,
                source_line=api.source_line,
                source_symbol=api.source_symbol,
                extractor=api_extractor,
                confidence=api.confidence,
                confidence_reason=api.confidence_reason,
                status="active",
                manual_override=False,
            )
            db.add(obj)
            stats.added += 1
            continue

        if obj.manual_override:
            stats.skipped_manual += 1
            continue

        changed = False
        updates = {
            "name": _display_name(api),
            "summary": api.summary or None,
            "operation_id": api.operation_id,
            "tags": api.tags,
            "source_file": api.source_file,
            "source_line": api.source_line,
            "source_symbol": api.source_symbol,
            "extractor": api_extractor,
            "confidence": api.confidence,
            "confidence_reason": api.confidence_reason,
        }
        for name, value in updates.items():
            if getattr(obj, name) != value:
                setattr(obj, name, value)
                changed = True
        if not _json_eq(obj.request_schema, api.request_schema):
            obj.request_schema = api.request_schema or {}
            changed = True
        if not _json_eq(obj.response_schema, api.response_schema):
            obj.response_schema = api.response_schema
            changed = True
        if obj.status != "active":
            obj.status = "active"
            changed = True
        if changed:
            stats.updated += 1
        else:
            stats.unchanged += 1

    active_sources = offline_extractors if offline_extractors is not None else {extractor}
    # 增量同步只解析变更文件，不能据此判断其他接口已消失
    for key, obj in (existing.items() if allow_offline else ()):
        if key in seen:
            continue
        if obj.manual_override:
            continue
        if obj.status != "active":
            continue
        if (obj.extractor or "") not in active_sources:
            continue
        obj.status = "offline"
        stats.removed += 1

    await db.flush()
    return stats


def binding_auth(binding: CodeRepoBinding) -> RepoAuth | None:
    """把绑定里的加密凭据还原成 RepoAuth。"""
    if not binding.auth_secret:
        return None
    try:
        secret = decrypt_value(binding.auth_secret)
    except Exception:
        raise GitSyncError("仓库凭据无法解密，请重新保存凭据") from None
    if binding.auth_type == "ssh_key":
        return RepoAuth(kind="ssh", secret=secret)
    username, token = _split_https_secret(secret)
    return RepoAuth(kind="https", username=username, secret=token)


def _split_https_secret(secret: str) -> tuple[str | None, str]:
    """`user:token` 形式拆出用户名；纯 token 时用户名留空（dulwich 用默认）。"""
    if ":" in secret and "\n" not in secret:
        head, tail = secret.split(":", 1)
        if head and " " not in head:
            return head, tail
    return None, secret


def _extract_all(
    *,
    binding: CodeRepoBinding,
    stack: TechStack,
    repo_dir: str,
    openapi_doc: dict | None,
    changed_files: list[str] | None = None,
    should_cancel=None,
    max_files: int | None = None,
) -> tuple[list[ExtractedApi], list[str], list[str]]:
    """按绑定偏好运行提取器；返回 (合并结果, 成功的提取器, 错误)。"""
    extractors = resolve_extractors(binding.extractor, stack)
    if not extractors:
        return [], [], ["未探测到可用的接口提取方式（既无 OpenAPI 文档，也未发现 Spring 控制器）"]

    source_roots = None
    if binding.source_root:
        source_roots = [binding.source_root]
    elif stack.source_roots:
        source_roots = stack.source_roots

    merged: dict[tuple[str, str], ExtractedApi] = {}
    succeeded: list[str] = []
    errors: list[str] = []
    for extractor in extractors:
        if should_cancel is not None and should_cancel():
            errors.append("提取被取消")
            break
        ctx = ExtractContext(
            repo_dir=repo_dir,
            source_roots=source_roots,
            changed_files=changed_files,
            should_cancel=should_cancel,
            max_files=max_files,
        )
        if extractor.name == "openapi":
            ctx.openapi_doc = openapi_doc
            ctx.openapi_url = binding.openapi_url
            if (
                ctx.openapi_doc is None
                and not ctx.openapi_url
                and stack.openapi_paths
                and not changed_files
            ):
                ctx.openapi_doc = _load_repo_openapi(repo_dir, stack.openapi_paths[0], errors)
            if ctx.openapi_doc is None and not ctx.openapi_url:
                # 增量模式且文档未变更：不重复解析，保留既有接口
                continue
        try:
            apis = extractor.extract(ctx)
        except ExtractorError as exc:
            errors.append(f"{extractor.name}: {exc}")
            continue
        except Exception as exc:  # 提取器自身异常不应中断整个同步
            logger.exception("提取器 %s 失败", extractor.name)
            errors.append(f"{extractor.name}: {exc}")
            continue
        succeeded.append(extractor.name)
        for api in apis:
            merged.setdefault((api.method, api.path), api)
    return list(merged.values()), succeeded, errors


def _load_repo_openapi(repo_dir: str, rel_path: str, errors: list[str]) -> dict | None:
    """从仓库读取 OpenAPI/Swagger 文档。"""
    path = Path(repo_dir) / rel_path
    try:
        from app.parsers.api_doc.common import _load_json_or_yaml

        doc = _load_json_or_yaml(path.read_bytes())
    except Exception as exc:
        errors.append(f"openapi: 读取 {rel_path} 失败：{exc}")
        return None
    if not isinstance(doc, dict):
        errors.append(f"openapi: {rel_path} 不是对象")
        return None
    return doc


def _reset_invalid_workdir(workdir: str) -> None:
    """清理上次克隆失败留下的无效目录（否则 clone 会因目录非空永久失败）。"""
    path = Path(workdir)
    if not path.is_dir():
        return
    if head_commit(workdir):
        return
    try:
        for child in path.iterdir():
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink(missing_ok=True)
        logger.info("清理无效仓库目录：%s", workdir)
    except OSError:
        logger.warning("清理无效仓库目录失败：%s", workdir, exc_info=True)


async def _plan_full_sync(
    db: AsyncSession, binding: CodeRepoBinding, base: str | None
) -> tuple[bool, int]:
    """决定本次是全量还是增量；返回 (是否全量, 已累计的增量次数)。"""
    if base is None:
        return True, 0
    every = int(getattr(binding, "full_sync_every", 0) or 0)
    if every <= 0:
        return False, 0
    last = (
        await db.execute(
            select(ApiSyncRecord)
            .where(
                ApiSyncRecord.project_id == binding.project_id,
                ApiSyncRecord.status.in_(("success", "no_change")),
            )
            .order_by(ApiSyncRecord.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    count = int(((last.details or {}).get("incremental_count") or 0)) if last else 0
    return count + 1 >= every, count


async def _finish_record(
    db: AsyncSession,
    record: ApiSyncRecord,
    binding: CodeRepoBinding,
    outcome: SyncOutcome,
    errors: list[str],
    *,
    incremental_count: int,
    full_sync: bool,
    cancelled: bool = False,
) -> None:
    """更新绑定与同步记录并提交。"""
    stack = outcome.tech
    binding.last_sync_at = tz_now()
    binding.last_commit = outcome.head_commit
    binding.last_error = outcome.error if outcome.status == "failed" else None

    record.base_commit = outcome.base_commit
    record.head_commit = outcome.head_commit
    record.finished_at = tz_now()
    record.added = outcome.stats.added
    record.updated = outcome.stats.updated
    record.removed = outcome.stats.removed
    record.status = outcome.status
    record.error = outcome.error
    if cancelled:
        record.cancelled_at = tz_now()
    record.details = {
        "extractors": outcome.extractors,
        "incremental": outcome.incremental,
        "full_sync": full_sync,
        "incremental_count": incremental_count,
        "changed_files": list(outcome.changed_files[:50]),
        "warnings": outcome.warnings,
        "errors": errors[:10],
        "tech": {
            "spring": bool(stack and stack.spring),
            "openapi": bool(stack and stack.openapi),
            "build_tool": stack.build_tool if stack else None,
            "source_roots": stack.source_roots if stack else [],
            "evidence": (stack.evidence[:10] if stack else []),
        },
        "stats": outcome.stats.as_dict(),
    }
    await db.commit()


async def _audit_sync(db: AsyncSession, project_id: int, trigger: str, outcome: SyncOutcome) -> None:
    try:
        from app.auth import log_audit

        await log_audit(
            db,
            None,
            f"api_sync.{trigger}",
            {
                "project_id": project_id,
                "status": outcome.status,
                "added": outcome.stats.added,
                "updated": outcome.stats.updated,
                "removed": outcome.stats.removed,
                "head_commit": outcome.head_commit,
                "incremental": outcome.incremental,
            },
        )
    except Exception:  # noqa: BLE001 - 审计失败不影响同步结果
        logger.warning("同步审计写入失败 project=%s", project_id, exc_info=True)


async def sync_binding(
    db: AsyncSession,
    binding: CodeRepoBinding,
    *,
    trigger: str = "manual",
    repo_dir: str | None = None,
    openapi_doc: dict | None = None,
    timeout: int | None = None,
) -> SyncOutcome:
    """执行一次仓库同步（含首次克隆/增量更新）并落同步记录。"""
    settings = get_settings()
    if not settings.api_extract_enabled:
        return SyncOutcome(status="disabled", error="接口提取已关闭")

    wait = timeout or settings.api_repo_clone_timeout_seconds
    project_id = int(binding.project_id)
    binding_id = int(binding.id)
    workdir = repo_dir or repo_workdir(project_id)

    cancel_event = threading.Event()
    _CANCEL_FLAGS[project_id] = cancel_event

    record = ApiSyncRecord(project_id=project_id, trigger=trigger, status="running")
    db.add(record)
    # 先落库 running 记录，前端可立即看到「同步中」并取消
    await db.commit()
    await db.refresh(record)

    def _cancelled() -> bool:
        return cancel_event.is_set()

    async def _check_cancel() -> bool:
        # 跨 worker 场景：刷新记录读取取消标志
        try:
            await db.refresh(record)
        except Exception:
            return cancel_event.is_set()
        if record.cancel_requested:
            cancel_event.set()
        return cancel_event.is_set()

    outcome = SyncOutcome()
    try:
        auth = binding_auth(binding)
        Path(workdir).mkdir(parents=True, exist_ok=True)
        _reset_invalid_workdir(workdir)
        base = head_commit(workdir)
        full_sync, incremental_count = await _plan_full_sync(db, binding, base)
        changed: list[str] = []

        if base is None:
            result = await to_thread(
                clone_repository,
                binding.repo_url,
                workdir,
                branch=binding.branch or None,
                auth=auth,
                timeout=wait,
            )
            outcome.head_commit = result.head_commit
        else:
            old, new = await to_thread(
                update_repository,
                workdir,
                binding.repo_url,
                branch=binding.branch or None,
                auth=auth,
                timeout=wait,
            )
            outcome.base_commit, outcome.head_commit = old, new
            if old and new and old != new:
                changed = await to_thread(list_changed_files, workdir, old, new)
            elif old == new:
                # 没有新提交，无需解析
                outcome.status = "no_change"
                outcome.changed_files = []
                await _finish_record(
                    db, record, binding, outcome, [], incremental_count=incremental_count,
                    full_sync=False,
                )
                await _audit_sync(db, project_id, trigger, outcome)
                return outcome

        if await _check_cancel():
            outcome.status = "cancelled"
            outcome.error = "同步已取消"
            await _finish_record(
                db, record, binding, outcome, [], incremental_count=incremental_count,
                full_sync=full_sync, cancelled=True,
            )
            await _audit_sync(db, project_id, trigger, outcome)
            return outcome

        max_files = int(getattr(settings, "api_extract_max_scan_files", 0) or 0)
        stack = await to_thread(detect_tech_stack, workdir, max_files=max_files or 2000)
        outcome.tech = stack
        incremental = bool(changed) and not full_sync and base is not None
        outcome.incremental = incremental
        outcome.changed_files = changed

        apis, succeeded, errors = await to_thread(
            _extract_all,
            binding=binding,
            stack=stack,
            repo_dir=workdir,
            openapi_doc=openapi_doc,
            changed_files=changed if incremental else None,
            should_cancel=_cancelled,
            max_files=max_files or None,
        )
        outcome.extractors = succeeded

        if await _check_cancel():
            outcome.status = "cancelled"
            outcome.error = "同步已取消"
            await _finish_record(
                db,
                record,
                binding,
                outcome,
                errors,
                incremental_count=incremental_count,
                full_sync=full_sync,
                cancelled=True,
            )
            await _audit_sync(db, project_id, trigger, outcome)
            return outcome

        if not succeeded:
            outcome.status = "failed"
            outcome.error = "；".join(errors) or "没有可用的提取器"
        else:
            outcome.stats = await apply_extracted_apis(
                db,
                project_id=project_id,
                apis=apis,
                extractor=succeeded[0],
                offline_extractors=set(succeeded),
                allow_offline=not incremental,
            )
            if not apis and not errors:
                outcome.status = "no_change"
            if errors:
                outcome.error = "；".join(errors)

        if max_files and len(stack.evidence) >= 10:
            outcome.warnings.append("技术栈探测可能因扫描上限截断，结果仅供参考")
        if incremental:
            outcome.warnings.append(
                f"增量同步：仅解析 {len(changed)} 个变更文件，未做下线判断"
            )

        next_count = 0 if not incremental else incremental_count + 1
        await _finish_record(
            db, record, binding, outcome, errors,
            incremental_count=next_count, full_sync=full_sync,
        )
        await _audit_sync(db, project_id, trigger, outcome)
    except Exception as exc:
        await db.rollback()
        logger.warning("同步仓库失败 project=%s: %s", project_id, exc)
        outcome.status = "failed"
        outcome.error = str(exc)
        # 失败也要留下记录（单独事务，避免与上面的回滚混淆）
        try:
            from sqlalchemy import update

            await db.execute(
                update(CodeRepoBinding)
                .where(CodeRepoBinding.id == binding_id)
                .values(
                    last_error=str(exc)[:1000],
                    last_sync_at=tz_now(),
                    updated_at=tz_now(),
                )
            )
            failed = ApiSyncRecord(
                project_id=project_id,
                trigger=trigger,
                finished_at=tz_now(),
                status="failed",
                error=str(exc)[:2000],
                base_commit=outcome.base_commit,
                head_commit=outcome.head_commit,
            )
            db.add(failed)
            await db.commit()
        except Exception:
            await db.rollback()
    finally:
        _CANCEL_FLAGS.pop(project_id, None)
    return outcome
