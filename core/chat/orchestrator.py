# core/chat/orchestrator.py
# 034 对话编排器（D0/D3/D9）：一轮消息 → LLM 决策 → 校验 → 动作分发 → 落库 + 推送。
#
# 原则：
#   - 动作级容错：单动作校验/执行失败只丢弃该动作并说明（不整条作废，032 教训）；
#   - 长任务由动作处理器返回卡片（queued/running），状态由各服务异步推进；
#   - 审计：每个成功执行的动作写 audit_logs（FR-006）。
from __future__ import annotations

import asyncio
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
    try:
        from app.runtime_config import chat_browse_config as _cb

        max_chars = int(getattr(_cb, "context_max_chars", max_chars) or max_chars)
        keep = int(getattr(_cb, "context_keep_messages", keep) or keep)
    except Exception:
        pass
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
        elif kind == "functional":
            parts.append(
                "工作类型：功能用例（本对话生成/修改功能用例；功能用例为手工用例、不支持自动执行——"
                "用户要求执行功能用例时不要输出 run_case，改为说明其只能手工执行或建议改用 UI 自动化用例）"
            )
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
    msgs = context.build_llm_messages(
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
    # 最后一道防线：装配后仍超预算 → 压缩较早消息内容（增量摘要之外的兜底）
    return context.compact_messages(msgs, _context_budget_chars())


def _context_budget_chars() -> int:
    """对话上下文预算（运行时配置优先，回退环境默认）。"""
    try:
        from app.runtime_config import chat_browse_config as _cb

        return int(getattr(_cb, "context_max_chars", 24000) or 24000)
    except Exception:
        return int(getattr(get_settings(), "chat_context_max_chars", 24000) or 24000)


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

def _tool_loop_enabled() -> bool:
    """035：工具循环开关（chat_tool_loop_enabled，默认开）。"""
    try:
        return bool(getattr(get_settings(), "chat_tool_loop_enabled", True))
    except Exception:
        return False


async def _tool_stream_callbacks(conversation, target_message_id: int | None = None):
    """工具循环流式回调：message.stream_start + thinking/reply 增量（与决策模式同通道）。

    content 双形态自适应：纯文本（工具模式正常回复）直接推送；
    决策 JSON（混合输出兼容）按 PartialDecisionStream 增量提取 reply/thinking。
    """
    from core.chat import stream as chat_stream

    conv_id = conversation.id
    reasoning_parts: list[str] = []
    state = {
        "mode": "unknown",  # unknown | text | json
        "buf": "",
        "stream": None,
        "t_len": 0,
        "r_len": 0,
    }
    reasoning_seen = {"v": False}

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
        reasoning_seen["v"] = True
        reasoning_parts.append(chunk)
        await push("thinking", chunk)

    async def on_content(chunk: str) -> None:
        if state["mode"] == "unknown":
            state["buf"] += chunk
            stripped = state["buf"].lstrip()
            if not stripped:
                return
            if stripped.startswith("{"):
                state["mode"] = "json"
                state["stream"] = chat_stream.PartialDecisionStream()
                state["stream"].feed(state["buf"])
                state["buf"] = ""
            else:
                state["mode"] = "text"
                await push("reply", state["buf"])
                state["buf"] = ""
                return
        elif state["mode"] == "text":
            await push("reply", chunk)
            return
        else:
            state["stream"].feed(chunk)
        if state["mode"] == "json":
            if not reasoning_seen["v"]:
                cur_t = state["stream"].get("thinking")
                if len(cur_t) > state["t_len"]:
                    await push("thinking", cur_t[state["t_len"]:])
                    state["t_len"] = len(cur_t)
            cur_r = state["stream"].get("reply")
            if len(cur_r) > state["r_len"]:
                await push("reply", cur_r[state["r_len"]:])
                state["r_len"] = len(cur_r)

    await emit(conv_id, {
        "type": "message.stream_start",
        "conversation_id": conv_id,
        "target_message_id": target_message_id,
    })
    return on_reasoning, on_content, reasoning_parts


async def handle_user_message(
    db,
    *,
    conversation,
    user_id: int,
    content: str,
    refs=None,
    attachments: Optional[list] = None,
    llm_call: Optional[Callable[[list], Awaitable[str]]] = None,
    user_message=None,
) -> tuple:
    """处理一条用户消息：落库 → LLM 决策 → 动作执行 → 助手消息落库。

    返回 (user_message, assistant_message)。``llm_call`` 可注入（测试/替换）。
    ``user_message``：编辑重生成场景传入已存在的用户消息（跳过落库与 message.created，
    内容已在调用前更新）。
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
    if user_message is None:
        refs_all = list(refs or [])
        for a in (attachments or [])[:5]:
            if isinstance(a, dict) and a.get("file_ref"):
                ref = {
                    "type": "file",
                    "file_ref": str(a["file_ref"])[:500],
                    "label": str(a.get("filename") or "")[:200],
                }
                # 035+：附件内容提取（docx/pdf/xlsx/md → 文本进上下文；受控路径 + 预算截断）
                try:
                    from app.services.chat_files import extract_attachment_text, resolve_chat_file

                    path = resolve_chat_file(ref["file_ref"])
                    if path:
                        max_chars = int(getattr(get_settings(), "chat_attachment_max_chars", 6000) or 6000)
                        text, err = await asyncio.to_thread(
                            extract_attachment_text, path, max_chars=max_chars
                        )
                        if text:
                            ref["text"] = text
                        elif err:
                            ref["text_error"] = err[:200]
                except Exception:
                    logger.debug("attachment extract failed", exc_info=True)
                refs_all.append(ref)
        user_msg = await chat_crud.create_message(
            db, conversation_id=conversation.id, role="user", content=content, refs=refs_all or None,
        )
        await emit(conversation.id, {"type": "message.created", "message": serialize_message(user_msg)})
    else:
        # 编辑重生成：消息已更新并推送 message.updated，本轮不重复落库
        user_msg = user_message
        history = [m for m in history if m.id < user_msg.id]

    # 3) 上下文组装（摘要 + 引用 + 草稿摘要）
    messages = list(history) + [user_msg]
    llm_messages = await _build_llm_messages(db, conversation, messages, llm, agent_id=agent_id)

    # 3.5) 035：原生工具循环（优先；网关不支持/开关关闭/测试注入 → 既有决策协议，C7）
    tool_outcome = None
    tool_thinking: Optional[str] = None
    if llm_call is None and _tool_loop_enabled():
        from app.gen.model_client import ToolCallingUnsupported
        from core.tools.loop import run_tool_loop
        from core.tools.registry import ToolContext

        try:
            on_reasoning, on_content, reasoning_parts = await _tool_stream_callbacks(conversation)
            tool_ctx = ToolContext(db=db, user_id=user_id, conversation=conversation, source="chat")

            async def _tool_llm(messages, *, tools, stream_callback=None, reasoning_callback=None):
                from app.gen.model_client import call_model as _cm

                return await _cm(messages, tools=tools, stream_callback=stream_callback,
                                 reasoning_callback=reasoning_callback,
                                 agent_type="assistant", agent_id=agent_id)

            tool_outcome = await run_tool_loop(
                db=db, conversation=conversation, ctx=tool_ctx, messages=llm_messages,
                llm=_tool_llm, on_content=on_content, on_reasoning=on_reasoning,
            )
            tool_thinking = "".join(reasoning_parts).strip() or None
        except ToolCallingUnsupported as exc:
            logger.info("tool loop unsupported → decision protocol fallback: %s", exc)
            tool_outcome = None
        except Exception as exc:
            logger.warning("tool loop failed: %s", exc, exc_info=True)
            assistant_msg = await chat_crud.create_message(
                db, conversation_id=conversation.id, role="assistant",
                content="（模型调用失败，请稍后重试）",
                actions=[{"type": "error", "status": "error", "error": str(exc)[:300]}],
            )
            await emit(conversation.id, {"type": "message.created", "message": serialize_message(assistant_msg)})
            return user_msg, assistant_msg

    if tool_outcome is not None:
        # 混合输出兼容（C7）：未用工具但内容为决策 JSON → 交决策协议执行
        if not tool_outcome.used_tools and tool_outcome.reply:
            _decision, _err = contracts.parse_decision(tool_outcome.reply)
            if _decision is not None:
                _decision = contracts.redact_decision_secrets(_decision)
                cards, actions_log, notes, task_id = await _execute_decision(
                    db, conversation, user_id, _decision, llm,
                )
                reply = _compose_reply(_decision["reply"], notes, cards)
                if dropped_refs:
                    reply = (reply + f"\n（失效引用已剔除：{'、'.join(dropped_refs[:3])}）").strip()
                assistant_msg = await chat_crud.create_message(
                    db, conversation_id=conversation.id, role="assistant",
                    content=reply, thinking=_decision.get("thinking") or tool_thinking,
                    cards=cards or None, actions=actions_log or None, task_id=task_id,
                )
                await emit(conversation.id, {"type": "message.created", "message": serialize_message(assistant_msg)})
                return user_msg, assistant_msg
        # 工具循环正常完成：落库（卡片/工具行/任务句柄来自循环）
        # 038 US4：回复/思考落库前做显示层密码打码（模型可能复述用户消息里的密码）
        from core.chat.mask import mask_text

        reply = mask_text(tool_outcome.reply)
        tool_thinking = mask_text(tool_thinking)
        if dropped_refs:
            reply = (reply + f"\n（失效引用已剔除：{'、'.join(dropped_refs[:3])}）").strip()
        assistant_msg = await chat_crud.create_message(
            db, conversation_id=conversation.id, role="assistant",
            content=reply, thinking=tool_thinking,
            cards=tool_outcome.cards or None,
            actions=tool_outcome.actions_log or None,
            task_id=tool_outcome.task_id,
        )
        await emit(conversation.id, {"type": "message.created", "message": serialize_message(assistant_msg)})
        return user_msg, assistant_msg

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


async def edit_message(
    db,
    *,
    conversation,
    message_id: int,
    content: str,
    user_id: int,
    llm_call: Optional[Callable[[list], Awaitable[str]]] = None,
) -> tuple:
    """编辑已发送的用户消息并重新生成（2026-10-14 新增能力）。

    语义：更新该用户消息内容 → 删除其后所有消息（含助手回复）→ 以新内容重跑本轮。
    返回 (user_message | None, assistant_message | None, error)。
    """
    msg = await chat_crud.get_message(db, conversation.id, message_id)
    if msg is None:
        return None, None, "消息不存在"
    if msg.role != "user":
        return None, None, "只能编辑用户消息"

    content = (content or "").strip()
    if not content:
        return None, None, "消息内容不能为空"

    # 1) 截断：删除该消息之后的全部消息（WS 同步推送 message.deleted，前端收缩列表）
    later = list(await chat_crud.list_messages_after(db, conversation.id, after_id=message_id))
    for m in later:
        await chat_crud.delete_message(db, m)
        await emit(conversation.id, {"type": "message.deleted", "message_id": m.id})

    # 2) 更新内容
    updated = await chat_crud.update_message(db, msg, content=content)
    await emit(conversation.id, {"type": "message.updated", "message": serialize_message(updated)})

    # 3) 以新内容重跑本轮（不重复落用户消息、不重复推 message.created）
    user_msg, assistant = await handle_user_message(
        db, conversation=conversation, user_id=user_id, content=content,
        llm_call=llm_call, user_message=updated,
    )
    return user_msg, assistant, ""


async def delete_message(db, *, conversation, message_id: int) -> tuple:
    """删除一条消息（不再参与上下文，FR-032）。返回 (ok, error)。"""
    msg = await chat_crud.get_message(db, conversation.id, message_id)
    if msg is None:
        return False, "消息不存在"
    await chat_crud.delete_message(db, msg)
    await emit(conversation.id, {"type": "message.deleted", "message_id": message_id})
    return True, ""
