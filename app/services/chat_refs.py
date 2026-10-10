# app/services/chat_refs.py
# 034 US6 引用机制：set_refs 动作 + 引用校验（存在性 / 项目权限 / 失效剔除）。
from __future__ import annotations

import logging

from app.crud import chat as chat_crud
from core.chat.orchestrator import ActionOutcome, register_action_handler

logger = logging.getLogger(__name__)

REF_TYPES = ("project", "environment", "agent", "case", "batch")


async def validate_refs(db, refs, user) -> tuple[list, list[str]]:
    """校验引用：存在性 + 项目权限。返回 (有效引用列表, 问题列表)。"""
    from app import db_models
    from app.auth import get_user_project_filter
    from app import crud

    allowed = get_user_project_filter(user)

    def _access(project_id) -> bool:
        return allowed is None or project_id in allowed

    valid: list = []
    problems: list[str] = []
    for r in refs or []:
        if not isinstance(r, dict):
            continue
        t, i = r.get("type"), r.get("id")
        if t not in REF_TYPES or not isinstance(i, int):
            problems.append(f"非法引用 {t}#{i}")
            continue
        if t == "case":
            tc = await crud.get_test_case(db, i)
            if tc is None:
                problems.append(f"用例 #{i} 不存在")
                continue
            if not _access(tc.project_id):
                problems.append(f"无权引用用例 #{i}（项目 {tc.project_id}）")
                continue
            valid.append({**r, "project_id": tc.project_id,
                          "case_kind": getattr(tc, "case_kind", None) or "ui"})
        elif t == "batch":
            b = await crud.get_run_batch(db, i)
            if b is None:
                problems.append(f"批次 #{i} 不存在")
                continue
            if not _access(b.project_id):
                problems.append(f"无权引用批次 #{i}（项目 {b.project_id}）")
                continue
            valid.append({**r, "project_id": b.project_id})
        elif t == "environment":
            env = await crud.get_environment(db, i)
            if env is None:
                problems.append(f"环境 #{i} 不存在")
                continue
            if not _access(env.project_id):
                problems.append(f"无权引用环境 #{i}（项目 {env.project_id}）")
                continue
            valid.append({**r, "project_id": env.project_id})
        elif t == "agent":
            agent = await db.get(db_models.Agent, i)
            if agent is None:
                problems.append(f"Agent #{i} 不存在")
                continue
            valid.append(dict(r))
        elif t == "project":
            p = await crud.get_project(db, i)
            if p is None:
                problems.append(f"项目 #{i} 不存在")
                continue
            if not _access(p.id):
                problems.append(f"无权引用项目 #{i}")
                continue
            valid.append(dict(r))
    return valid, problems


def merge_refs(current: list, add: list, remove: list) -> list:
    """按 (type,id) 去重合并；remove 后应用 add（后者优先）。"""
    def _key(r):
        return (r.get("type"), r.get("id"))

    removals = {_key(r) for r in remove or []}
    merged = [dict(r) for r in (current or []) if _key(r) not in removals]
    existing = {_key(r) for r in merged}
    for r in add or []:
        if _key(r) not in existing:
            merged.append(dict(r))
            existing.add(_key(r))
    return merged[:20]


async def prune_invalid_refs(db, refs) -> tuple[list, list[str]]:
    """存在性剔除（删除/失效标记）；不做权限判断（权限在写入时校验）。"""
    valid: list = []
    dropped: list[str] = []
    for r in refs or []:
        if not isinstance(r, dict):
            continue
        t, i = r.get("type"), r.get("id")
        if t == "case":
            from app import crud

            tc = await crud.get_test_case(db, i)
            if tc is None:
                dropped.append(f"{t}#{i}")
                continue
        elif t == "batch":
            from app import crud

            b = await crud.get_run_batch(db, i)
            if b is None:
                dropped.append(f"{t}#{i}")
                continue
        valid.append(dict(r))
    return valid, dropped


@register_action_handler("set_refs")
async def set_refs_handler(db, conversation, action: dict, ctx: dict) -> ActionOutcome:
    """更新对话级引用（add/remove），逐项校验存在性 + 权限。"""
    from app import db_models

    user = await db.get(db_models.User, ctx.get("user_id"))
    if user is None:
        return ActionOutcome(ok=False, note="（用户不存在）")

    valid_add, problems = await validate_refs(db, action.get("add"), user)
    if problems and not valid_add:
        return ActionOutcome(ok=False, note="（引用未生效：" + "；".join(problems[:3]) + "）")

    new_refs = merge_refs(
        getattr(conversation, "refs", None) or [],
        valid_add,
        action.get("remove") or [],
    )
    await chat_crud.update_conversation(db, conversation, refs=new_refs or None)
    note = "引用已更新。"
    if problems:
        note += "（部分未生效：" + "；".join(problems[:3]) + "）"
    return ActionOutcome(note=note)
