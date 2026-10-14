# core/chat/orchestrator.py
# 034 对话编排器（D0/D3/D9）：一轮消息 → LLM 决策 → 校验 → 动作分发 → 落库 + 推送。
#
# 原则：
#   - 动作级容错：单动作校验/执行失败只丢弃该动作并说明（不整条作废，032 教训）；
#   - 长任务由动作处理器返回卡片（queued/running），状态由各服务异步推进；
#   - 审计：每个成功执行的动作写 audit_logs（FR-006）。
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

from app.config import get_settings
from app.crud import chat as chat_crud
from core.chat import contracts, context
from core.chat.events import emit

logger = logging.getLogger(__name__)


@dataclass
class ActionOutcome:
    """动作处理器返回值：卡片 + 追加说明 + 任务 id。"""

    card: Optional[dict] = None
    note: str = ""
    ok: bool = True
    task_id: Optional[str] = None


ACTION_HANDLERS: dict[str, Callable] = {}


def register_action_handler(action_type: str):
    """动作处理器注册装饰器（US2+ 各故事注册 generate/revise_drafts/...）。"""

    def deco(fn):
        ACTION_HANDLERS[action_type] = fn
        return fn

    return deco


# ----------------------------
# 序列化 / 小工具
# ----------------------------

def serialize_message(msg) -> dict:
    def _iso(v):
        return v.isoformat() if v is not None else None

    return {
        "id": msg.id,
        "conversation_id": msg.conversation_id,
        "role": msg.role,
        "content": msg.content,
        "thinking": getattr(msg, "thinking", None),
        "cards": msg.cards or [],
        "refs": msg.refs or [],
        "actions": msg.actions or [],
        "task_id": msg.task_id,
        "created_at": _iso(getattr(msg, "created_at", None)),
        "updated_at": _iso(getattr(msg, "updated_at", None)),
    }


async def _project_name(db, project_id: int) -> Optional[str]:
    try:
        from sqlalchemy import select

        from app import db_models

        return (
            await db.execute(select(db_models.Project.name).where(db_models.Project.id == project_id))
        ).scalar_one_or_none()
    except Exception:
        return None


def _active_draft_digest(messages) -> Optional[str]:
    """从历史中找最近一个未导入的草稿集卡片，生成给模型的草稿清单。"""
    for m in reversed(list(messages)):
        for c in reversed(context._attr(m, "cards", None) or []):
            if isinstance(c, dict) and c.get("type") == "draft_set" and c.get("status") != "imported":
                items = c.get("items") or []
                lines = [
                    f"【当前草稿集】session={c.get('gen_session_id', '')}"
                    f"（{c.get('case_kind', '')}）共 {len(items)} 条："
                ]
                for it in items[:50]:
                    lines.append(f"- draft_id={it.get('draft_id')} {str(it.get('title') or '')[:80]}")
                return "\n".join(lines)
    return None


async def _prune_conversation_refs(db, conversation):
    """US6：注入前剔除失效引用（不存在的用例/批次），返回失效说明。"""
    refs = getattr(conversation, "refs", None)
    if not refs:
        return conversation, []
    try:
        from app.services import chat_refs

        valid, dropped = await chat_refs.prune_invalid_refs(db, refs)
        if dropped:
            await chat_crud.update_conversation(db, conversation, refs=valid or None)
            return conversation, dropped
    except Exception:
        logger.debug("prune refs failed", exc_info=True)
    return conversation, []


async def resolve_system_prompt(db, conversation, *, agent_id=None) -> str:
    """对话助手系统提示词：DB 模板（可被 Agent 覆盖）→ 内置兜底。"""
    from app.runtime_config import resolve_prompt_for_agent
    from core.chat.prompts import CHAT_ASSISTANT_PROMPT

    aid = agent_id if agent_id is not None else getattr(conversation, "assistant_agent_id", None)
    try:
        text = await resolve_prompt_for_agent(db, "assistant", "chat_assistant", agent_id=aid)
        if text and text.strip():
            return text
    except Exception:
        logger.debug("resolve chat system prompt failed", exc_info=True)
    return CHAT_ASSISTANT_PROMPT


async def _maybe_update_summary(db, conversation, messages, llm) -> tuple[Optional[str], list]:
    """超预算时增量摘要较早消息；返回 (summary, 供注入的消息列表)。"""
    settings = get_settings()
    max_chars = int(getattr(settings, "chat_context_max_chars", 24000) or 24000)
    keep = int(getattr(settings, "chat_summary_keep_messages", 12) or 12)
    total = context.estimate_context_chars(messages)
    if total <= max_chars:
        return conversation.summary, list(messages)
    older, recent = context.split_for_summary(messages, keep)
    if not older:
        return conversation.summary, list(messages)
    cutoff = conversation.summarized_until_message_id or 0
    to_summarize = [m for m in older if int(context._attr(m, "id", 0) or 0) > cutoff]
    if not to_summarize:
        return conversation.summary, recent
    chunk = await context.summarize_history(llm, to_summarize)
    merged = ((conversation.summary + "\n" if conversation.summary else "") + chunk).strip()[:8000]
    last_id = int(context._attr(to_summarize[-1], "id", 0) or 0)
    await chat_crud.update_conversation(
        db, conversation, summary=merged or None,
        summarized_until_message_id=last_id or None,
    )
    return merged or None, recent


def _redact_action(action: dict) -> dict:
    """审计用：递归打码敏感字段（password 等），疑似凭据只留掩码。"""
    def _walk(value):
        if isinstance(value, dict):
            return {
                k: ("******" if str(k).lower() in ("password", "secret", "token") else _walk(v))
                for k, v in value.items()
            }
        if isinstance(value, list):
            return [_walk(v) for v in value]
        return value

    return _walk(dict(action or {}))


async def _audit(db, user_id: int, conversation_id: int, action: dict, outcome: ActionOutcome) -> None:
    try:
        from app.auth import log_audit

        await log_audit(
            db, user_id, f"chat.{action['type']}",
            details={
                "conversation_id": conversation_id,
                "action": _redact_action(action),
                "status": "ok" if outcome.ok else "failed",
            },
        )
    except Exception:
        logger.debug("chat audit failed", exc_info=True)


async def _conversation_settings_block(db, conversation) -> Optional[str]:
    """【当前对话设置】注入块：工作类型 / 默认执行环境 / 默认客户端（对话页选择）。"""
    try:
        parts = []
        kind = (getattr(conversation, "case_kind", None) or "ui").lower()
        if kind == "api":
            parts.append("工作类型：接口用例（本对话执行接口用例；接口用例的生成请在「接口测试」页完成）")
        else:
            parts.append("工作类型：UI 自动化用例")
        env_id = getattr(conversation, "environment_id", None)
        if env_id:
            try:
                from app import db_models

                env = await db.get(db_models.Environment, int(env_id))
                if env is not None:
                    url = (getattr(env, "base_url", "") or "").strip()
                    parts.append(f"默认执行环境：# {env.id} {env.name}" + (f"（{url}）" if url else ""))
            except Exception:
                logger.debug("settings env lookup failed", exc_info=True)
        agent_id = getattr(conversation, "agent_id", None)
        if agent_id:
            try:
                from app import db_models

                ag = await db.get(db_models.Agent, int(agent_id))
                if ag is not None:
                    parts.append(f"默认执行客户端：{ag.name}（客户端形态运行）")
            except Exception:
                logger.debug("settings agent lookup failed", exc_info=True)
        else:
            parts.append("默认执行客户端：服务端")
        parts.append(
            "执行类动作默认使用上述环境与客户端，不要询问也不要自行更换；"
            "仅当用户在本条消息中明确要求更换时才在动作里显式传 environment_id / agent_id / mode。"
        )
        return "【当前对话设置】" + "；".join(parts)
    except Exception:
        logger.debug("build settings block failed", exc_info=True)
        return None


async def _build_llm_messages(db, conversation, messages, llm, *, agent_id=None) -> list[dict]:
    summary, inject_messages = await _maybe_update_summary(db, conversation, messages, llm)
    draft_digest = _active_draft_digest(inject_messages)
    system_prompt = await resolve_system_prompt(db, conversation, agent_id=agent_id)
    project_name = None
    environments = None
    if getattr(conversation, "project_id", None):
        project_name = await _project_name(db, conversation.project_id)
        try:
            from app.crud import get_environments

            envs = await get_environments(db, conversation.project_id)
            environments = [
                {"id": e.id, "name": e.name, "base_url": e.base_url}
                for e in (envs or [])
            ]
        except Exception:
            logger.debug("load chat environments failed", exc_info=True)
    settings_block = await _conversation_settings_block(db, conversation)
    suites = None
    if getattr(conversation, "project_id", None):
        try:
            from app.crud.suite import list_suites

            _suites = await list_suites(db, conversation.project_id)
            suites = [
                {
                    "id": s.id,
                    "name": s.name,
                    "case_kind": getattr(s, "case_kind", "ui"),
                    "count": len(list(getattr(s, "cases", None) or [])),
                }
                for s in (_suites or [])
            ]
        except Exception:
            logger.debug("load chat suites failed", exc_info=True)
    return context.build_llm_messages(
        system_prompt=system_prompt,
        summary=summary,
        messages=inject_messages,
        refs=getattr(conversation, "refs", None),
        project_name=project_name,
        draft_digest=draft_digest,
        environments=environments,
        suites=suites,
        settings_block=settings_block,
    )


async def _execute_decision(db, conversation, user_id: int, decision: dict, llm) -> tuple:
    """校验并执行一轮决策的全部动作；返回 (cards, actions_log, notes, task_id)。"""
    cards: list = []
    actions_log: list = []
    notes: list = []
    task_id: Optional[str] = None
    for action in decision["actions"]:
        norm, aerr = contracts.validate_action(action)
        if aerr:
            notes.append(f"（{aerr}，已忽略该动作）")
            actions_log.append({
                "type": action.get("type") if isinstance(action, dict) else None,
                "status": "rejected", "error": aerr,
            })
            continue
        handler = ACTION_HANDLERS.get(norm["type"])
        if handler is None:
            notes.append(f"（{norm['type']} 能力尚未开放）")
            actions_log.append({"type": norm["type"], "status": "unimplemented"})
            continue
        try:
            outcome = await handler(db, conversation, norm, {"user_id": user_id, "llm": llm})
        except Exception as exc:
            logger.exception("chat action failed: %s", norm["type"])
            actions_log.append({"type": norm["type"], "status": "error", "error": str(exc)[:300]})
            notes.append(f"（{norm['type']} 执行失败：{str(exc)[:120]}）")
            continue
        if outcome.card:
            cards.append(outcome.card)
        actions_log.append({
            "type": norm["type"],
            "status": "ok" if outcome.ok else "failed",
            "note": outcome.note or None,
        })
        if outcome.task_id and task_id is None:
            task_id = outcome.task_id
        if outcome.note:
            notes.append(outcome.note)
        await _audit(db, user_id, conversation.id, norm, outcome)
    return cards, actions_log, notes, task_id


def _compose_reply(reply: str, notes: list, cards: list) -> str:
    if notes:
        reply = (reply + "\n" + "\n".join(n for n in notes if n)).strip()
    if not reply and not cards:
        reply = "（我暂时无法处理这个请求，请换个说法或补充信息）"
    return reply


# ----------------------------
# 主入口
async def _stream_llm(
    conversation,
    llm_messages: list[dict],
    *,
    agent_id: int | None,
    target_message_id: int | None = None,
) -> str:
    """流式决策调用：实时推送 thinking / reply 增量（WS），返回完整文本。

    - thinking 通道：优先使用网关 reasoning 增量（真实思考）；网关不回推理时退回
      决策 JSON 的 thinking 字段增量（部分 JSON 提取）；
    - reply 通道：决策 JSON 的 reply 字段增量（部分 JSON 提取）；
    - 推送是尽力而为（WS 未连接则自然丢弃），失败不影响正常取回完整文本。
    """
    from app.gen.model_client import call_model

    from core.chat import stream as chat_stream

    conv_id = conversation.id
    partial = chat_stream.PartialDecisionStream()
    emitted = {"thinking": 0, "reply": 0}
    flags = {"reasoning": False}

    async def push(channel: str, text: str) -> None:
        if not text:
            return
        await emit(conv_id, {
            "type": "message.stream",
            "conversation_id": conv_id,
            "channel": channel,
            "delta": text,
            "target_message_id": target_message_id,
        })

    async def on_reasoning(chunk: str) -> None:
        flags["reasoning"] = True
        await push("thinking", chunk)

    async def on_content(chunk: str) -> None:
        partial.feed(chunk)
        if not flags["reasoning"]:
            cur = partial.get("thinking")
            if len(cur) > emitted["thinking"]:
                await push("thinking", cur[emitted["thinking"]:])
                emitted["thinking"] = len(cur)
        cur = partial.get("reply")
        if len(cur) > emitted["reply"]:
            await push("reply", cur[emitted["reply"]:])
            emitted["reply"] = len(cur)

    await emit(conv_id, {
        "type": "message.stream_start",
        "conversation_id": conv_id,
        "target_message_id": target_message_id,
    })
    return await call_model(
        llm_messages,
        agent_type="assistant",
        agent_id=agent_id,
        stream_callback=on_content,
        reasoning_callback=on_reasoning,
    )


# ----------------------------

async def handle_user_message(
    db,
    *,
    conversation,
    user_id: int,
    content: str,
    refs=None,
    attachments: Optional[list] = None,
    llm_call: Optional[Callable[[list], Awaitable[str]]] = None,
) -> tuple:
    """处理一条用户消息：落库 → LLM 决策 → 动作执行 → 助手消息落库。

    返回 (user_message, assistant_message)。``llm_call`` 可注入（测试/替换）。
    """
    from app.gen.model_client import call_model

    agent_id = getattr(conversation, "assistant_agent_id", None)

    async def _default_llm(messages):
        return await call_model(messages, agent_type="assistant", agent_id=agent_id)

    llm = llm_call or _default_llm

    # 1) 历史（先取，避免包含刚落的用户消息）
    history = await chat_crud.list_messages_after(db, conversation.id)

    # US6：剔除失效引用（不存在的用例/批次）后再组装上下文
    conversation, dropped_refs = await _prune_conversation_refs(db, conversation)

    # 2) 落用户消息（附件以 file 引用形式随消息存储，供上下文注入）
    refs_all = list(refs or [])
    for a in (attachments or [])[:5]:
        if isinstance(a, dict) and a.get("file_ref"):
            refs_all.append({
                "type": "file",
                "file_ref": str(a["file_ref"])[:500],
                "label": str(a.get("filename") or "")[:200],
            })
    user_msg = await chat_crud.create_message(
        db, conversation_id=conversation.id, role="user", content=content, refs=refs_all or None,
    )
    await emit(conversation.id, {"type": "message.created", "message": serialize_message(user_msg)})

    # 3) 上下文组装（摘要 + 引用 + 草稿摘要）
    messages = list(history) + [user_msg]
    llm_messages = await _build_llm_messages(db, conversation, messages, llm, agent_id=agent_id)

    # 4) LLM 决策（生产路径走流式：thinking/reply 实时推送；测试注入 llm_call 时走阻塞路径）
    try:
        if llm_call is not None:
            raw = await llm(llm_messages)
        else:
            try:
                raw = await _stream_llm(conversation, llm_messages, agent_id=agent_id)
            except Exception:
                logger.warning("chat stream llm failed, fallback to blocking call", exc_info=True)
                raw = await llm(llm_messages)
    except Exception as exc:
        logger.warning("chat llm call failed: %s", exc)
        assistant_msg = await chat_crud.create_message(
            db, conversation_id=conversation.id, role="assistant",
            content="（模型调用失败，请稍后重试）",
            actions=[{"type": "error", "status": "error", "error": str(exc)[:300]}],
        )
        await emit(conversation.id, {"type": "message.created", "message": serialize_message(assistant_msg)})
        return user_msg, assistant_msg

    decision, err = contracts.parse_decision(raw)
    _diag: Optional[str] = None
    if decision is None:
        # 一次纠错重试：模型偶发只输出自然语言而漏掉 JSON 契约（实测：要求"执行用例集"时
        # 连续两轮只回话不动作）——追加纠错提示再要一轮 JSON，仍失败才走兜底。
        try:
            retry_messages = llm_messages + [{
                "role": "user",
                "content": (
                    "你上一轮输出不是契约要求的决策 JSON。请严格只输出一个 JSON 对象"
                    "（不要任何其他文字、不要代码块），把本轮该执行的动作放进 actions。"
                ),
            }]
            if llm_call is not None:
                raw2 = await llm(retry_messages)
            else:
                try:
                    raw2 = await _stream_llm(conversation, retry_messages, agent_id=agent_id)
                except Exception:
                    logger.warning("chat stream retry failed, fallback to blocking call", exc_info=True)
                    raw2 = await llm(retry_messages)
            decision2, _err2 = contracts.parse_decision(raw2)
            if decision2 is not None:
                decision, raw = decision2, raw2
            else:
                _diag = (raw or "")[:400] + "\n---retry---\n" + (raw2 or "")[:400]
                raw = raw2 or raw  # 展示用：优先重试后的输出
        except Exception:
            logger.warning("chat JSON retry failed", exc_info=True)
    if decision is None:
        # 文本兜底：模型直接输出自然语言（未按 JSON 契约）时保留其回复，不丢弃
        fallback = contracts._text_fallback(raw)
        if fallback:
            assistant_msg = await chat_crud.create_message(
                db, conversation_id=conversation.id, role="assistant",
                content=fallback,
                actions=[{"type": "text_fallback", "status": "ok", "raw": (_diag or raw or "")[:800]}],
            )
            await emit(conversation.id, {"type": "message.created", "message": serialize_message(assistant_msg)})
            return user_msg, assistant_msg
        assistant_msg = await chat_crud.create_message(
            db, conversation_id=conversation.id, role="assistant",
            content="（未能理解模型输出，请重试）",
            actions=[{"type": "error", "status": "error", "error": err, "raw": (raw or "")[:800]}],
        )
        await emit(conversation.id, {"type": "message.created", "message": serialize_message(assistant_msg)})
        return user_msg, assistant_msg

    # 5) 动作校验与执行（动作级容错）
    decision = contracts.redact_decision_secrets(decision)
    cards, actions_log, notes, task_id = await _execute_decision(db, conversation, user_id, decision, llm)

    # 6) 助手消息落库
    reply = _compose_reply(decision["reply"], notes, cards)
    if dropped_refs:
        reply = (reply + f"\n（失效引用已剔除：{'、'.join(dropped_refs[:3])}）").strip()
    assistant_msg = await chat_crud.create_message(
        db, conversation_id=conversation.id, role="assistant",
        content=reply, thinking=decision.get("thinking") or None,
        cards=cards or None, actions=actions_log or None, task_id=task_id,
    )
    await emit(conversation.id, {"type": "message.created", "message": serialize_message(assistant_msg)})
    return user_msg, assistant_msg


async def regenerate_message(
    db,
    *,
    conversation,
    message_id: int,
    user_id: int,
    llm_call: Optional[Callable[[list], Awaitable[str]]] = None,
) -> tuple:
    """重新生成一条助手消息（替换该条；不回溯后续轮次，FR-032）。

    返回 (updated_message | None, error)。
    """
    from app.gen.model_client import call_model

    msg = await chat_crud.get_message(db, conversation.id, message_id)
    if msg is None:
        return None, "消息不存在"
    if msg.role != "assistant":
        return None, "只能重新生成助手消息"
    agent_id = getattr(conversation, "assistant_agent_id", None)

    async def _default_llm(messages):
        return await call_model(messages, agent_type="assistant", agent_id=agent_id)

    llm = llm_call or _default_llm

    history = [m for m in await chat_crud.list_messages_after(db, conversation.id) if m.id < msg.id]
    llm_messages = await _build_llm_messages(db, conversation, history, llm, agent_id=agent_id)
    try:
        if llm_call is not None:
            raw = await llm(llm_messages)
        else:
            try:
                raw = await _stream_llm(
                    conversation, llm_messages, agent_id=agent_id, target_message_id=msg.id,
                )
            except Exception:
                logger.warning("chat stream regenerate failed, fallback to blocking call", exc_info=True)
                raw = await llm(llm_messages)
    except Exception as exc:
        return None, f"模型调用失败：{exc}"
    decision, err = contracts.parse_decision(raw)
    if decision is None:
        return None, err
    cards, actions_log, notes, task_id = await _execute_decision(db, conversation, user_id, decision, llm)
    reply = _compose_reply(decision["reply"], notes, cards)
    updated = await chat_crud.update_message(
        db, msg, content=reply, thinking=decision.get("thinking") or None,
        cards=cards or None, actions=actions_log or None,
    )
    if task_id:
        updated.task_id = task_id
        await db.commit()
    await emit(conversation.id, {"type": "message.updated", "message": serialize_message(updated)})
    return updated, ""


async def delete_message(db, *, conversation, message_id: int) -> tuple:
    """删除一条消息（不再参与上下文，FR-032）。返回 (ok, error)。"""
    msg = await chat_crud.get_message(db, conversation.id, message_id)
    if msg is None:
        return False, "消息不存在"
    await chat_crud.delete_message(db, msg)
    await emit(conversation.id, {"type": "message.deleted", "message_id": message_id})
    return True, ""
