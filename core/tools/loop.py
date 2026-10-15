# core/tools/loop.py
# 035 工具循环：原生 tool-calling 多轮循环（C1/C4/C6）。
#
# 流程：调用模型（携带 tools）→ 有 tool_calls 则执行（read 并行 / mutate+execute 串行）
#       → 结果回填（role=tool）→ 继续下一轮；无 tool_calls 则以内容为最终回复。
# 事件：tool.call.start / tool.call.end（C4，旧前端静默忽略）。
# 预算：轮次（chat_tool_max_rounds）/ 时长（chat_tool_timeout_seconds）；超出终止并说明。
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field, replace
from types import SimpleNamespace
from typing import Any, Callable, Optional

from app.config import get_settings
from core.chat.events import emit
from core.tools.registry import REGISTRY, ToolContext, ToolSpec
from core.tools.result import (
    TRUNCATION_GUIDANCE,
    ToolResult,
    error_result,
    result_bytes,
    shape_result,
)

logger = logging.getLogger(__name__)

_REDACT_KEYS = ("password", "secret", "token", "api_key", "apikey", "authorization")


def _redact(value):
    """递归打码敏感键（供摘要/审计复用）。"""
    if isinstance(value, dict):
        return {
            k: ("******" if str(k).lower() in _REDACT_KEYS else _redact(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_redact(v) for v in value]
    return value


def _canonical_args(raw: str) -> str:
    """规范化参数（排序键）——防循环同参判定用。"""
    try:
        obj = json.loads(raw or "{}")
    except Exception:
        return str(raw or "")[:400]
    try:
        return json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str)[:400]
    except Exception:
        return str(raw or "")[:400]


@dataclass
class LoopOutcome:
    """一轮工具循环的结果（交编排层落库）。"""

    reply: str = ""
    cards: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    task_id: Optional[str] = None
    actions_log: list = field(default_factory=list)
    rounds: int = 0
    used_tools: bool = False


def _args_digest(args: dict) -> str:
    """参数摘要（敏感键打码，≤200 字符）——供 WS 事件/日志/审计。"""
    try:
        text = json.dumps(_redact(dict(args or {})), ensure_ascii=False, default=str)
    except Exception:
        text = "{}"
    return text[:200]


async def _audit_tool_call(ctx: ToolContext, name: str, args: dict, result: ToolResult) -> None:
    """每次工具调用写审计行 tool.<name>（参数打码；失败仅 debug 不阻断）。"""
    try:
        from app.auth import log_audit

        await log_audit(
            ctx.db,
            ctx.user_id,
            f"tool.{name}",
            details={
                "conversation_id": getattr(getattr(ctx, "conversation", None), "id", None),
                "source": getattr(ctx, "source", "chat"),
                "token_id": getattr(ctx, "token_id", None),
                "args": _redact(dict(args or {})),
                "status": "ok" if result.ok else "failed",
            },
        )
    except Exception:
        logger.debug("tool audit failed name=%s", name, exc_info=True)


def _tool_message_content(result: ToolResult) -> str:
    text = json.dumps(result.to_dict(), ensure_ascii=False, default=str)
    if result.truncated:
        text += "\n" + TRUNCATION_GUIDANCE
    return text


async def _run_one_tool(*, ctx: ToolContext, conversation_id: int, call,
                        spec: Optional[ToolSpec], round_no: int) -> tuple[str, ToolResult]:
    """执行单个工具调用：解析参数 → 执行 → 预算守卫 → 事件。返回 (call_id, result)。"""
    settings = get_settings()
    call_id = getattr(call, "id", "") or f"call_{round_no}_{abs(hash(getattr(call, 'name', ''))) % 100000}"
    name = getattr(call, "name", "") or ""
    start = time.monotonic()

    args: dict = {}
    parse_error: Optional[str] = None
    raw_args = getattr(call, "arguments", "") or ""
    try:
        args = json.loads(raw_args) if raw_args.strip() else {}
        if not isinstance(args, dict):
            raise ValueError("参数必须是 JSON 对象")
    except Exception as exc:
        parse_error = f"参数解析失败：{exc}"

    await emit(conversation_id, {
        "type": "tool.call.start",
        "conversation_id": conversation_id,
        "call_id": call_id,
        "tool": name,
        "args_digest": _args_digest(args),
        "round": round_no,
    })

    if parse_error:
        result = error_result(parse_error)
    elif spec is None:
        result = error_result(f"未知工具：{name}")
    else:
        try:
            result = await spec.handler(ctx, **args)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("tool %s failed: %s", name, exc, exc_info=True)
            result = error_result(f"工具执行失败：{exc}")
        if not isinstance(result, ToolResult):
            result = error_result("工具未返回 ToolResult")

    # 预算守卫：handler 未走 shape_result 时兜底（禁止静默截断，SC-008）
    max_bytes = int(getattr(settings, "chat_tool_result_max_bytes", 16384) or 16384)
    if not result.truncated and result_bytes(result.data) > max_bytes:
        result = shape_result(result.data, summary=result.summary, max_bytes=max_bytes)

    # 审计（T029）：每次调用写 tool.<name>（参数打码；失败不阻断）
    await _audit_tool_call(ctx, name, args, result)

    duration_ms = int((time.monotonic() - start) * 1000)
    await emit(conversation_id, {
        "type": "tool.call.end",
        "conversation_id": conversation_id,
        "call_id": call_id,
        "ok": bool(result.ok),
        "summary": (result.summary or "")[:200],
        "duration_ms": duration_ms,
        "truncated": bool(result.truncated),
    })
    return call_id, result


async def _execute_calls(*, ctx: ToolContext, conversation_id: int, calls: list,
                         specs: dict, round_no: int) -> list[tuple[str, ToolResult]]:
    """一轮工具执行：read 并行（≤ chat_tool_parallel_max），mutate/execute 串行且不与只读交叉。

    并行只读使用**独立 session**（SQLAlchemy AsyncSession 不支持并发共享；
    单只读调用沿用请求 session，零额外开销）。
    """
    settings = get_settings()
    results: dict[int, tuple] = {}

    read_idx = [
        i for i, c in enumerate(calls)
        if specs.get(getattr(c, "name", "")) is not None
        and specs[getattr(c, "name", "")].side_effect == "read"
    ]
    read_set = set(read_idx)
    serial_idx = [i for i in range(len(calls)) if i not in read_set]

    if read_idx:
        sem = asyncio.Semaphore(max(1, int(getattr(settings, "chat_tool_parallel_max", 4) or 4)))
        use_own_sessions = len(read_idx) > 1 and ctx.db is not None

        async def _read(i: int) -> None:
            async with sem:
                call = calls[i]
                if use_own_sessions:
                    from app.database import AsyncSessionLocal

                    async with AsyncSessionLocal() as db_i:
                        results[i] = await _run_one_tool(
                            ctx=replace(ctx, db=db_i), conversation_id=conversation_id,
                            call=call, spec=specs.get(getattr(call, "name", "")), round_no=round_no,
                        )
                else:
                    results[i] = await _run_one_tool(
                        ctx=ctx, conversation_id=conversation_id,
                        call=call, spec=specs.get(getattr(call, "name", "")), round_no=round_no,
                    )

        await asyncio.gather(*(_read(i) for i in read_idx))

    for i in serial_idx:
        results[i] = await _run_one_tool(
            ctx=ctx, conversation_id=conversation_id,
            call=calls[i], spec=specs.get(getattr(calls[i], "name", "")), round_no=round_no,
        )

    return [results[i] for i in range(len(calls))]


async def _default_llm(messages, *, tools, stream_callback=None, reasoning_callback=None):
    """默认模型调用（生产路径由编排层注入带 agent 配置的 wrapper）。"""
    from app.gen.model_client import call_model

    return await call_model(
        messages, tools=tools,
        stream_callback=stream_callback, reasoning_callback=reasoning_callback,
        agent_type="assistant",
    )


async def run_tool_loop(
    *,
    db,
    conversation,
    ctx: ToolContext,
    messages: list,
    llm: Optional[Callable] = None,
    on_content: Optional[Callable] = None,
    on_reasoning: Optional[Callable] = None,
    registry=None,
    max_rounds: Optional[int] = None,
    timeout_s: Optional[int] = None,
) -> LoopOutcome:
    """多轮工具循环。``llm(messages, *, tools, stream_callback, reasoning_callback) -> ModelReply``。

    无工具注册或网关拒绝 tools 时抛 ``ToolCallingUnsupported``（调用方降级，C7）。
    """
    from app.gen.model_client import ToolCallingUnsupported

    settings = get_settings()
    reg = registry if registry is not None else REGISTRY
    specs = {s.name: s for s in reg.all()}
    if not specs:
        raise ToolCallingUnsupported("无注册工具")

    tools_payload = reg.as_openai_tools()
    max_rounds = int(max_rounds or getattr(settings, "chat_tool_max_rounds", 8) or 8)
    timeout_s = int(timeout_s or getattr(settings, "chat_tool_timeout_seconds", 180) or 180)
    deadline = time.monotonic() + timeout_s

    call_llm = llm or _default_llm
    conv_id = conversation.id
    outcome = LoopOutcome()
    call_counts: dict[str, int] = {}

    for round_no in range(1, max_rounds + 1):
        outcome.rounds = round_no
        if time.monotonic() > deadline:
            outcome.notes.append(f"任务超出预算（时长 > {timeout_s}s），已终止")
            break

        reply = await call_llm(
            messages, tools=tools_payload,
            stream_callback=on_content, reasoning_callback=on_reasoning,
        )

        if isinstance(reply, str):
            # 兼容桥：注入/包装的模型调用未走 tools 协议（返回纯文本）→ 视为最终回复，
            # 由编排层决定"混合输出（决策 JSON）"或"纯文本回答"（C7）。
            outcome.reply = reply
            return outcome

        if not reply.tool_calls:
            outcome.reply = reply.content or ""
            return outcome

        outcome.used_tools = True
        # 统一 call id（模型可能缺 id；三处消费保持一致）
        norm_calls = [
            SimpleNamespace(
                id=getattr(c, "id", "") or f"call_{round_no}_{i}",
                name=getattr(c, "name", "") or "",
                arguments=getattr(c, "arguments", "") or "{}",
            )
            for i, c in enumerate(reply.tool_calls)
        ]
        assistant_msg = {
            "role": "assistant",
            "content": reply.content or "",
            "tool_calls": [
                {
                    "id": c.id,
                    "type": "function",
                    "function": {"name": c.name, "arguments": c.arguments},
                }
                for c in norm_calls
            ],
        }
        messages.append(assistant_msg)

        pairs = await _execute_calls(
            ctx=ctx, conversation_id=conv_id, calls=norm_calls,
            specs=specs, round_no=round_no,
        )

        call_by_id = {c.id: c for c in norm_calls}
        stop_reason: Optional[str] = None
        for call_id, result in pairs:
            call = call_by_id.get(call_id)
            name = getattr(call, "name", "") or ""

            # 防循环（T028）：同工具同参数计数 → ≥3 提示 / ≥5 终止
            sig = f"{name}::{_canonical_args(getattr(call, 'arguments', '') or '{}')}"
            call_counts[sig] = call_counts.get(sig, 0) + 1
            repeat_n = call_counts[sig]
            reminder = ""
            if repeat_n >= 5:
                stop_reason = f"重复调用（同工具同参数 ≥5 次：{name}），已终止"
            elif repeat_n == 3:
                reminder = (
                    f"\n[治理提示] 相同调用（{name}，同参数）已重复 3 次，结果不会变化——"
                    "请勿再重复调用；信息不足时请换用其他工具或直接给出结论。"
                )

            # 提取大对象（卡片/任务 id）交编排层落库；不回喂模型
            if isinstance(result.data, dict) and ("card" in result.data or "task_id" in result.data):
                data = dict(result.data)
                card = data.pop("card", None)
                tid = data.pop("task_id", None)
                if card:
                    outcome.cards.append(card)
                if tid and outcome.task_id is None:
                    outcome.task_id = str(tid)
                result = ToolResult(
                    ok=result.ok, summary=result.summary, data=data or None,
                    truncated=result.truncated, next_cursor=result.next_cursor, error=result.error,
                )
            messages.append({
                "role": "tool",
                "tool_call_id": call_id,
                "content": _tool_message_content(result) + reminder,
            })
            outcome.actions_log.append({
                "type": "tool",
                "tool": name,
                "status": "ok" if result.ok else "failed",
                "summary": (result.summary or "")[:300],
                "truncated": bool(result.truncated),
            })

        if stop_reason:
            outcome.notes.append(stop_reason)
            break

        if time.monotonic() > deadline:
            outcome.notes.append(f"任务超出预算（时长 > {timeout_s}s），已终止")
            break
    else:
        outcome.notes.append(f"已达到工具调用轮次上限（{max_rounds} 轮）")

    if not outcome.reply:
        base = "（任务未在预算内完成）"
        if outcome.notes:
            base = "（任务未在预算内完成：" + "；".join(outcome.notes) + "）"
        outcome.reply = base
    return outcome
