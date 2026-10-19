# app/services/chat_gen.py
# 034 对话内生成（US2）：
#   - start_generation：复用既有生成管线（文字描述 / 上传文档），同源落 gen_sessions；
#   - 生成任务 watcher：轮询 DB 进度 → 卡片状态回填 + WS 推送（生成串行由编排器任务模型保证）；
#   - revise_drafts：草稿增量补丁（C4，原子应用 + structured_steps 重算）；
#   - import_drafts：确认导入（复用既有导入语义）。
from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from io import BytesIO
from typing import Optional

from app import crud
from app.crud import chat as chat_crud
from core.chat import contracts
from core.chat.events import emit
from core.chat.orchestrator import ActionOutcome, register_action_handler

logger = logging.getLogger(__name__)

_CHAT_UPLOAD_DIR = os.path.join("data", "chat_uploads")


# ----------------------------
# 卡片读写
# ----------------------------

def _draft_item(tc) -> dict:
    steps = [s.strip() for s in (tc.test_steps or "").splitlines() if s.strip()]
    return {
        "draft_id": tc.id,
        "title": tc.title or "",
        "steps": steps,
        "expected": tc.expected_result or "",
        "priority": tc.priority or "",
        "module": tc.module or "",
        "state": None,
    }


async def _find_card_message(db, conversation_id: int, gen_session_id: str):
    msgs = await chat_crud.list_messages_after(db, conversation_id)
    for m in reversed(msgs):
        for c in reversed(m.cards or []):
            if isinstance(c, dict) and c.get("type") == "draft_set" and c.get("gen_session_id") == gen_session_id:
                return m, c
    return None, None


async def _write_card(db, message, card: dict) -> None:
    cards = []
    replaced = False
    for c in message.cards or []:
        if isinstance(c, dict) and c.get("type") == "draft_set" and c.get("gen_session_id") == card.get("gen_session_id"):
            cards.append(card)
            replaced = True
        else:
            cards.append(c)
    if not replaced:
        cards.append(card)
    updated = await chat_crud.update_message_cards(db, message, cards)
    from core.chat.orchestrator import serialize_message

    await emit(updated.conversation_id, {"type": "message.updated", "message": serialize_message(updated)})


# ----------------------------
# 生成启动（复用既有管线）
# ----------------------------

def _resolve_chat_file(file_ref: str) -> Optional[str]:
    """把附件 file_ref 解析为受控路径（仅允许 data/chat_uploads 之下）。"""
    if not file_ref:
        return None
    normalized = os.path.normpath(file_ref)
    base = os.path.normpath(_CHAT_UPLOAD_DIR)
    if not normalized.startswith(base + os.sep):
        return None
    if not os.path.isfile(normalized):
        return None
    return normalized


async def _pick_generation_agent(db, kind: str):
    """按用途选择活跃生成 Agent（ui → tc_generate_ui；functional → tc_generate）。"""
    from sqlalchemy import select

    from app import db_models

    rows = (
        await db.execute(
            select(db_models.AgentDefinition)
            .where(
                db_models.AgentDefinition.agent_type == "generation",
                db_models.AgentDefinition.is_active == 1,
            )
            .order_by(db_models.AgentDefinition.id)
        )
    ).scalars().all()
    want = "tc_generate_ui" if kind == "ui" else "tc_generate"
    for a in rows:
        if want in (a.skills or []):
            return a
    if rows:
        return rows[0]
    from app.crud.agent_definition import get_active_by_type

    return await get_active_by_type(db, "generation")


async def start_generation(
    db,
    *,
    conversation,
    kind: str,
    source: str,
    description: str,
    file_ref: Optional[str],
    user_id: int,
) -> tuple[Optional[dict], str, Optional[str]]:
    """启动对话生成；返回 (card, error, task_id)。"""
    from app.gen.models import AnalysisSession
    from app.gen.prompts import (
        case_kind_from_tc_prompt_key,
        min_tcs_per_item,
        pick_fp_prompt_key,
        pick_tc_prompt_key,
    )
    from app.routers.gen import upload as gen_upload
    from app.routers.gen.storage import save_session_files

    # 1) 生成 Agent 与提示词 key（复用既有选择语义）
    agent = await _pick_generation_agent(db, kind)
    skills = list(agent.skills or []) if agent else []
    fp_key = pick_fp_prompt_key(skills)
    tc_key = pick_tc_prompt_key(skills)
    min_tcs = min_tcs_per_item(skills, tc_prompt_key=tc_key)
    case_kind = case_kind_from_tc_prompt_key(tc_key)

    # 2) 输入内容（文字 → 虚拟 .md 文档；文件 → 受控路径读取）
    raw_bytes: list[bytes] = []
    filenames: list[str] = []
    if source == "file":
        path = _resolve_chat_file(file_ref or "")
        if path is None:
            return None, "附件不存在或非法", None
        with open(path, "rb") as fh:
            raw_bytes.append(fh.read())
        filenames.append(os.path.basename(path))
    else:
        text = (description or "").strip()
        if not text:
            return None, "缺少需求描述", None
        raw_bytes.append(text.encode("utf-8"))
        filenames.append("对话需求描述.md")

    session_id = str(uuid.uuid4())
    try:
        save_session_files(session_id, filenames, raw_bytes)
    except Exception:
        logger.exception("chat gen save files failed")
        return None, "保存生成输入失败", None

    project_description = (
        f"浏览器探索生成（会话 #{conversation.id}）"
        if source == "browse"
        else f"对话生成（会话 #{conversation.id}）"
    )
    analysis = AnalysisSession(
        session_id=session_id,
        filename=filenames[0],
        filenames=filenames,
        project_description=project_description,
        status="analyzing",
        case_kind=case_kind,
    )
    async with gen_upload._lock:
        gen_upload._sessions[session_id] = analysis

    await crud.create_gen_session(
        db,
        session_id=session_id,
        filename=filenames[0],
        filenames=json.dumps(filenames, ensure_ascii=False),
        project_id=conversation.project_id,
        project_description=project_description,
        user_id=user_id,
        case_kind=case_kind,
    )

    file_contents = [BytesIO(b) for b in raw_bytes]
    await gen_upload.launch_gen_analysis(
        session_id=session_id,
        session=analysis,
        file_contents=file_contents,
        filenames=filenames,
        project_description=project_description,
        selected_agent=agent,
        resolved_agent_id=agent.id if agent else None,
        resolved_skills=skills,
        fp_prompt_key=fp_key,
        tc_prompt_key=tc_key,
        min_tcs=min_tcs,
        capture_page_truth=False,
        project_id=conversation.project_id,
        environment_id=None,
        case_kind=case_kind,
    )

    asyncio.create_task(_watch_generation(conversation.id, session_id))
    card = {
        "type": "draft_set",
        "gen_session_id": session_id,
        "case_kind": case_kind,
        "status": "generating",
        "progress": 0,
        "items": [],
    }
    return card, "", session_id


async def _watch_generation(conversation_id: int, session_id: str, *, max_iterations: int = 900) -> None:
    """轮询生成会话状态 → 回填草稿卡片 + WS 推送（终态后退出）。"""
    from app.database import AsyncSessionLocal

    consecutive_errors = 0
    missing_card = 0
    for _ in range(max_iterations):
        await asyncio.sleep(2.0)
        try:
            async with AsyncSessionLocal() as db:
                record = await crud.get_gen_session(db, session_id)
                if record is None:
                    return
                status = record.status
                msg, card = await _find_card_message(db, conversation_id, session_id)
                if msg is None or card is None:
                    # 卡片消息可能滞后落库（035 工具循环下：generate_cases 工具返回后循环
                    # 还要跑最后一轮 LLM 才落助手消息）——宽限 60 轮（≈120s）再放弃，
                    # 避免 watcher 早退导致卡片永远停在「生成中」（2026-10-15 实测）。
                    missing_card += 1
                    if missing_card >= 60:
                        return
                    continue
                missing_card = 0
                consecutive_errors = 0
                if status in ("pending", "analyzing"):
                    card = {
                        **card,
                        "status": "generating",
                        "progress": record.progress or 0,
                        "progress_message": record.progress_message or "",
                    }
                    await _write_card(db, msg, card)
                    await emit(conversation_id, {
                        "type": "task.updated", "task_id": session_id, "kind": "generate",
                        "status": "running", "progress": record.progress or 0,
                    })
                    continue
                if status == "completed":
                    # 完成后的草稿清理（去重/噪声过滤/空标题派生；幂等）
                    try:
                        await _cleanup_completed_drafts(db, record)
                    except Exception:
                        logger.debug("draft cleanup failed session=%s", session_id, exc_info=True)
                    tcs = await crud.list_gen_test_cases(db, session_id)
                    card = {**card, "status": "draft", "progress": 100, "items": [_draft_item(tc) for tc in tcs]}
                elif status == "cancelled":
                    # 用户「停止」：卡片标记为已停止（区别于生成失败）
                    card = {**card, "status": "cancelled", "error": None}
                else:
                    card = {**card, "status": "failed", "error": (record.error_message or status)[:500]}
                await _write_card(db, msg, card)
                await emit(conversation_id, {
                    "type": "task.updated", "task_id": session_id, "kind": "generate",
                    "status": "done" if status == "completed" else (
                        "cancelled" if status == "cancelled" else "failed"
                    ),
                })
                return
        except Exception:
            consecutive_errors += 1
            logger.debug("watch generation failed session=%s consecutive=%s", session_id, consecutive_errors, exc_info=True)
            if consecutive_errors >= 15:
                return


# ----------------------------
# 生成完成后的草稿清理（2026-10-19：去重 / 空壳剔除 / 探索元信息噪声过滤）
# ----------------------------

# 报告元信息短语（仅对「浏览器探索生成」来源生效）：这些字样来自我们自己的
# 观察报告/工具描述，被误当页面元素生成的用例必为垃圾（用户实测批次）
_BROWSE_META_MARKERS = (
    "页面探索观察报告",
    "完成后生成UI用例草稿",
    "起始站点",
    "站内链接",
    "探索过程",
    "网络请求异常",
    "结束原因",
    "探索结论",
    "主要功能点",
    "已观察页面",
    "生成要求",
)


def _norm_sig_text(s) -> str:
    """签名归一：去空白/标点/序号，仅保留有效字符（用于重复判定）。"""
    import re as _re

    return _re.sub(r"[\s\d\.、，,：:；;（）()【】\[\]「」『』\-—_]+", "", str(s or "")).lower()


def _draft_signature(title: str, steps_text: str) -> str:
    return f"{_norm_sig_text(title)}::{_norm_sig_text(steps_text)}"


def _norm_step_template(t: str) -> str:
    """步骤模板归一：「断言页面包含X」「断言X可见」「等待X出现」→「断言X」。

    同一断言的不同模板措辞（实测样本：「断言页面包含【项目管理】」vs「断言【项目管理】可见」）
    应视为等价——重复判定只看断言目标。
    """
    out = str(t or "")
    out = out.replace("页面包含", "").replace("可见", "").replace("出现", "")
    out = out.replace("等待", "断言")
    return out


def _case_core_steps(steps: list) -> list:
    """用例核心步骤（去掉开头的「打开/访问」导航步骤后归一）。

    重复判定只比较核心部分——不同用例的差异通常只在首个导航步骤/措辞。
    """
    import re as _re

    core: list[str] = []
    for s in steps or []:
        txt = str(s).strip()
        if not core and _re.match(r"^\d+[\.、]?\s*(打开|访问|前往)", txt):
            continue
        norm = _norm_step_template(_norm_sig_text(txt))
        if norm:
            core.append(norm)
    return core


def _is_subseq(small: list, big: list) -> bool:
    """small 是否为 big 的有序子序列（用于「薄用例 ⊂ 厚用例」判定）。"""
    it = iter(big)
    return all(any(x == y for y in it) for x in small)


def _title_ratio(a: str, b: str) -> float:
    from difflib import SequenceMatcher

    return SequenceMatcher(None, a, b).ratio()


_HOLLOW_STEP_RE = None


def _is_hollow_case(title: str, steps: list) -> bool:
    """空心用例：唯一一步只是「打开/访问」页面，且标题承诺了未执行的行为。

    如标题「顶栏语言入口切换界面语言」但步骤只有「1. 打开首页」——用例会在执行时
    失败或空转，属生成噪声（2026-10-19 实测样本）。标题含 打开/访问/查看/校验/页面
    的薄用例（如「打开 dashboard 页面」）保留。
    """
    import re as _re

    if len(steps or []) != 1:
        return False
    only = str(steps[0]).strip()
    if not _re.match(r"^\d+[\.、]?\s*(打开|访问|前往)", only):
        return False
    return not _re.search(r"(打开|访问|查看|校验|页面)", str(title or ""))


def _is_browse_meta_junk(title: str, steps_text: str) -> bool:
    blob = _norm_sig_text(f"{title}\n{steps_text}")
    return any(_norm_sig_text(m) in blob for m in _BROWSE_META_MARKERS)


async def _cleanup_completed_drafts(db, record) -> int:
    """生成完成后的草稿清理：空壳/空心剔除 + 探索来源噪声过滤 + 重复合并 + 空标题派生。

    2026-10-19 用户实测：探索生成的草稿大量重复（「打开首页」×8；同一功能点
    多条相似用例），且把报告元信息当作页面元素。三层清理：
    ① 基础：空壳/元信息噪声/空标题派生（全来源）；
    ② 精确去重：标题+步骤签名完全相同（全来源）；
    ③ 近重复合并（仅探索来源）：核心步骤等价/包含、标题相似度 ≥0.85、
       空心单步用例——组内保留核心步骤最长的用例。
    返回删除条数（幂等：对已清理的会话再跑无副作用）。
    """
    tcs = await crud.list_gen_test_cases(db, record.id)
    browse_src = str(getattr(record, "project_description", "") or "").startswith("浏览器探索生成")
    seen: set[str] = set()
    removed = 0
    remaining: list = []
    for tc in tcs:
        steps = [s.strip() for s in (tc.test_steps or "").splitlines() if s.strip()]
        title = (tc.title or "").strip()
        if not title and not steps:
            await crud.delete_gen_test_case_raw(db, tc.id)
            removed += 1
            continue
        if browse_src and _is_browse_meta_junk(title, tc.test_steps or ""):
            await crud.delete_gen_test_case_raw(db, tc.id)
            removed += 1
            continue
        if browse_src and _is_hollow_case(title, steps):
            await crud.delete_gen_test_case_raw(db, tc.id)
            removed += 1
            continue
        if not title and steps:
            # 空标题 → 用首个步骤派生（导入后标题不再为空；用户反馈「还没有标题」）
            import re as _re

            derived = _re.sub(r"^\d+[\.、]\s*", "", steps[0])[:60]
            await crud.update_gen_test_case_raw(db, tc.id, {"title": derived})
            title = derived
        sig = _draft_signature(title, tc.test_steps or "")
        if sig in seen:
            await crud.delete_gen_test_case_raw(db, tc.id)
            removed += 1
            continue
        seen.add(sig)
        remaining.append(tc)

    # ③ 近重复合并（仅探索来源）
    if browse_src and len(remaining) > 1:
        entries = []
        for tc in remaining:
            steps = [s.strip() for s in (tc.test_steps or "").splitlines() if s.strip()]
            entries.append({
                "tc": tc,
                "core": _case_core_steps(steps),
                "ntitle": _norm_sig_text(tc.title or ""),
            })
        parent = list(range(len(entries)))

        def _find(i):
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        for i in range(len(entries)):
            for j in range(i + 1, len(entries)):
                a, b = entries[i], entries[j]
                dup = False
                if a["core"] and b["core"] and a["core"] == b["core"]:
                    dup = True
                elif (
                    a["core"] and b["core"] and len(a["core"]) != len(b["core"])
                ):
                    short, long_ = (
                        (a["core"], b["core"]) if len(a["core"]) < len(b["core"]) else (b["core"], a["core"])
                    )
                    if _is_subseq(short, long_):
                        dup = True
                if not dup and a["ntitle"] and b["ntitle"] and _title_ratio(a["ntitle"], b["ntitle"]) >= 0.85:
                    dup = True
                if dup:
                    ra, rb = _find(i), _find(j)
                    if ra != rb:
                        parent[rb] = ra
        groups: dict[int, list[int]] = {}
        for i in range(len(entries)):
            groups.setdefault(_find(i), []).append(i)
        for members in groups.values():
            if len(members) <= 1:
                continue
            # 组内保留核心步骤最长者（并列取最早）
            keep = max(members, key=lambda i: (len(entries[i]["core"]), -i))
            for i in members:
                if i != keep:
                    await crud.delete_gen_test_case_raw(db, entries[i]["tc"].id)
                    removed += 1

    if removed:
        logger.info(
            "chat gen cleanup session=%s removed=%s browse=%s",
            record.id, removed, browse_src,
        )
    return removed


# ----------------------------
# 草稿补丁（revise_drafts）
# ----------------------------

def _steps_to_text(steps: list) -> str:
    return "\n".join(f"{i + 1}. {s}" for i, s in enumerate(steps or []))


def _structured_from_steps(steps: list) -> Optional[list]:
    """按描述重算 structured_steps（研究影响 #1：避免导入快照与编辑脱节）。"""
    try:
        from core.step_normalize import parse_instant_to_structured

        out = []
        for s in steps or []:
            parsed = parse_instant_to_structured(str(s))
            if isinstance(parsed, dict):
                out.append(parsed)
        return out or None
    except Exception:
        return None


async def _persist_draft_changes(db, session_id: str, old_cases: list[dict], new_cases: list[dict]) -> None:
    """把补丁结果落库：update（含 structured 重算）/ add / remove。"""
    old_by_id = {c.get("draft_id"): c for c in old_cases if c.get("draft_id")}
    new_ids = {c.get("draft_id") for c in new_cases if c.get("draft_id")}

    # remove
    for did in list(old_by_id.keys()):
        if did not in new_ids:
            await crud.delete_gen_test_case_raw(db, did)

    for c in new_cases:
        did = c.get("draft_id")
        if did and did in old_by_id:
            old = old_by_id[did]
            fields: dict = {}
            if c.get("title") != old.get("title"):
                fields["title"] = c.get("title") or old.get("title")
            if c.get("expected") != old.get("expected"):
                fields["expected_result"] = c.get("expected")
            if c.get("priority") and c.get("priority") != old.get("priority"):
                fields["priority"] = c.get("priority")
            if c.get("module") and c.get("module") != old.get("module"):
                fields["module"] = c.get("module")
            if (c.get("steps") or []) != (old.get("steps") or []):
                fields["test_steps"] = _steps_to_text(c.get("steps") or [])
                fields["structured_steps"] = _structured_from_steps(c.get("steps") or [])
            if fields:
                await crud.update_gen_test_case_raw(db, did, fields)
        elif c.get("_new"):
            await crud.create_gen_test_case_raw(
                db,
                session_id,
                test_case_id=f"chat-{uuid.uuid4().hex[:8]}",
                module=c.get("module") or "",
                title=c.get("title") or "未命名",
                test_steps=_steps_to_text(c.get("steps") or []),
                expected_result=c.get("expected") or "",
                priority=c.get("priority") or "中",
                structured_steps=_structured_from_steps(c.get("steps") or []),
            )


def _describe_changes(changes: list) -> str:
    parts = []
    for ch in changes:
        if ch.get("kind") == "add":
            parts.append(f"新增「{ch.get('title') or '未命名'}」")
        elif ch.get("kind") == "remove":
            parts.append(f"删除「{ch.get('title') or ch.get('draft_id')}」")
        elif ch.get("kind") == "update":
            fields = ch.get("fields") or []
            parts.append(f"更新 draft#{ch.get('draft_id')}（{'/'.join(fields) if fields else '无变化'}）")
    return "；".join(parts) or "无实际变化"


@register_action_handler("revise_drafts")
async def revise_drafts_handler(db, conversation, action: dict, ctx: dict) -> ActionOutcome:
    session_id = action["gen_session_id"]
    msg, card = await _find_card_message(db, conversation.id, session_id)
    if card is None:
        return ActionOutcome(ok=False, note=f"（找不到草稿集 {session_id}）")
    tcs = await crud.list_gen_test_cases(db, session_id)
    old_cases = [_draft_item(tc) for tc in tcs]
    new_cases, changes, err = contracts.apply_draft_patch(old_cases, action["patch"])
    if err:
        return ActionOutcome(ok=False, note=f"（草稿修订未应用：{err}）")
    await _persist_draft_changes(db, session_id, old_cases, new_cases)
    tcs2 = await crud.list_gen_test_cases(db, session_id)
    card = {**card, "status": "draft", "items": [_draft_item(tc) for tc in tcs2], "last_changes": changes}
    await _write_card(db, msg, card)
    return ActionOutcome(note=f"草稿已更新：{_describe_changes(changes)}")


async def _has_active_generation(db, conversation_id: int) -> bool:
    """同对话生成串行（FR-031）：存在未终态的 draft_set 卡片即视为进行中。"""
    msgs = await chat_crud.list_messages_after(db, conversation_id)
    for m in reversed(msgs[-50:]):
        for c in (m.cards or []):
            if isinstance(c, dict) and c.get("type") == "draft_set" and c.get("status") in ("generating", "queued"):
                return True
    return False


@register_action_handler("generate")
async def generate_handler(db, conversation, action: dict, ctx: dict) -> ActionOutcome:
    if await _has_active_generation(db, conversation.id):
        return ActionOutcome(
            ok=False,
            note="（当前对话已有生成任务进行中，请等待它完成后再发起新的生成）",
        )
    card, err, task_id = await start_generation(
        db,
        conversation=conversation,
        kind=action["kind"],
        source=action["source"],
        description=action["payload"].get("description") or "",
        file_ref=action["payload"].get("file_ref"),
        user_id=ctx.get("user_id"),
    )
    if card is None:
        return ActionOutcome(ok=False, note=f"（生成未启动：{err}）")
    return ActionOutcome(card=card, task_id=task_id, note="生成任务已启动，完成后可直接确认导入。")


# ----------------------------
# 确认导入
# ----------------------------

async def import_drafts(
    db,
    *,
    conversation,
    gen_session_id: str,
    draft_ids: Optional[list] = None,
) -> tuple[Optional[dict], str]:
    """把草稿集（全部或指定）导入对话绑定的项目；返回 (result, error)。"""
    msg, card = await _find_card_message(db, conversation.id, gen_session_id)
    if card is None:
        return None, "草稿集不存在"
    record = await crud.get_gen_session(db, gen_session_id)
    if record is None:
        return None, "生成会话不存在"
    if record.status != "completed":
        return None, "分析尚未完成"
    project_id = conversation.project_id
    if project_id is None:
        return None, "对话未绑定项目"

    tcs = await crud.list_gen_test_cases(db, gen_session_id)
    selected_ids = None
    if draft_ids:
        id_set = set(draft_ids)
        tcs = [tc for tc in tcs if tc.id in id_set]
        if not tcs:
            return None, "没有可导入的草稿"
        selected_ids = [tc.test_case_id for tc in tcs]

    from app.gen.models import TestCase as GenTestCaseModel

    data = [
        GenTestCaseModel(
            test_case_id=tc.test_case_id,
            session_id=gen_session_id,
            module=tc.module or "",
            title=tc.title or "",
            preconditions=tc.preconditions or "",
            test_steps=tc.test_steps or "",
            expected_result=tc.expected_result or "",
            priority=tc.priority or "中",
            structured_steps=list(tc.structured_steps or [])
            if isinstance(getattr(tc, "structured_steps", None), list)
            else [],
        )
        for tc in tcs
    ]

    from app.gen.adapter import import_test_cases as do_import

    case_kind = record.case_kind if record.case_kind in ("functional", "ui") else "ui"
    created, skipped = await do_import(
        db, project_id, data, selected_ids, parent_module_id=None, case_kind=case_kind,
    )
    await crud.increment_imported_count(db, gen_session_id, project_id, len(created))

    imported_ids = [tc.id for tc in created]
    all_count = len(await crud.list_gen_test_cases(db, gen_session_id))
    card = {
        **card,
        "status": "imported" if len(imported_ids) + (record.imported_count or 0) >= all_count else "partial",
        "imported_case_ids": imported_ids,
    }
    await _write_card(db, msg, card)
    return {
        "imported_count": len(created),
        "skipped_count": skipped,
        "test_case_ids": imported_ids,
        "case_kind": case_kind,
        "project_id": project_id,
        "card": card,
    }, ""
