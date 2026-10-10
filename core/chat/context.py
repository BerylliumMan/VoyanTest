# core/chat/context.py
# 034 对话上下文组装与摘要（FR-004/005，D9）：
#   - 组装：system（助手提示词 + 项目上下文 + 引用 + 草稿摘要 + 历史摘要）+ 历史消息；
#   - 摘要：超出字符预算时压缩较早消息（增量：summary + summarized_until_message_id）；
#   - 关键引用常驻注入，永不参与压缩（SC-010）。
from __future__ import annotations

MAX_MESSAGE_CHARS = 8000


def _attr(msg, name, default=None):
    if isinstance(msg, dict):
        return msg.get(name, default)
    return getattr(msg, name, default)


def message_text(msg) -> str:
    """从 ORM 消息或 dict 提取注入文本（含引用与卡片一行摘要）。"""
    content = (_attr(msg, "content") or "")[:MAX_MESSAGE_CHARS]
    refs = _attr(msg, "refs", None) or []
    lines = []
    for r in refs:
        if not isinstance(r, dict):
            continue
        if r.get("type") == "file":
            lines.append(f"- 文件 {r.get('label') or ''} (file_ref={r.get('file_ref')})")
        else:
            lines.append(f"- {r.get('type')}#{r.get('id')} {r.get('label') or ''}".rstrip())
    if lines:
        content += "\n【引用】\n" + "\n".join(lines)
    digest = cards_digest(_attr(msg, "cards") or [])
    if digest:
        return (content + "\n" + digest).strip()
    return content


def cards_digest(cards) -> str:
    parts = []
    for c in cards or []:
        if not isinstance(c, dict):
            continue
        t = c.get("type")
        if t == "draft_set":
            items = c.get("items") or []
            parts.append(
                f"[草稿集 {c.get('case_kind', '')} 共 {len(items)} 条 | session={c.get('gen_session_id', '')} 状态={c.get('status', '')}]"
            )
        elif t == "diff":
            parts.append(f"[修改对照 case#{c.get('case_id')} 状态={c.get('status')}]")
        elif t == "run":
            parts.append(f"[执行批次#{c.get('batch_id')} 状态={c.get('status')}]")
        elif t == "evidence":
            parts.append(f"[失败分析 批次#{c.get('batch_id')}]")
    return "\n".join(parts)


def estimate_context_chars(messages) -> int:
    return sum(len(message_text(m)) for m in messages)


def split_for_summary(messages, keep: int):
    """返回 (较早消息, 最近 keep 条)。"""
    msgs = list(messages)
    if keep <= 0:
        return msgs, []
    if len(msgs) <= keep:
        return [], msgs
    return msgs[:-keep], msgs[-keep:]


def build_refs_block(refs) -> str:
    lines = []
    for r in refs or []:
        if not isinstance(r, dict):
            continue
        if r.get("type") == "file":
            lines.append(f"- 文件 {r.get('label') or r.get('file_ref')}")
            continue
        label = str(r.get("label") or "").strip()
        lines.append(f"- {r.get('type')}#{r.get('id')} {label}".rstrip())
    if not lines:
        return ""
    return "【当前引用】\n" + "\n".join(lines)


def build_environments_block(environments) -> str:
    """项目执行环境清单（对话内指定环境执行用；仅渲染 id/名称/地址）。"""
    lines = []
    for e in environments or []:
        if not isinstance(e, dict):
            continue
        try:
            eid = int(e.get("id"))
        except (TypeError, ValueError):
            continue
        name = str(e.get("name") or "").strip() or f"环境#{eid}"
        url = str(e.get("base_url") or "").strip()[:60]
        lines.append(f"- 环境#{eid} {name}" + (f"（{url}）" if url else ""))
    if not lines:
        return ""
    return "【项目环境 — 用户指定环境执行时 environment_id 只能取自本列表】\n" + "\n".join(lines)


def build_llm_messages(
    *,
    system_prompt: str,
    summary: str | None = None,
    messages,
    refs=None,
    project_name: str | None = None,
    draft_digest: str | None = None,
    environments=None,
    settings_block: str | None = None,
) -> list[dict]:
    """组装发给 LLM 的 messages（system 注入上下文块 + 历史消息）。"""
    system = system_prompt or ""
    ctx_parts = []
    if project_name:
        ctx_parts.append(f"【项目上下文】{project_name}")
    if settings_block:
        ctx_parts.append(settings_block)
    env_block = build_environments_block(environments)
    if env_block:
        ctx_parts.append(env_block)
    refs_block = build_refs_block(refs)
    if refs_block:
        ctx_parts.append(refs_block)
    if draft_digest:
        ctx_parts.append(draft_digest)
    if summary:
        ctx_parts.append(f"【较早对话摘要】\n{summary}")
    if ctx_parts:
        system = system + "\n\n" + "\n\n".join(ctx_parts)
    out = [{"role": "system", "content": system}]
    for m in messages:
        role = _attr(m, "role", "user")
        role = "assistant" if role == "assistant" else "user"
        out.append({"role": role, "content": message_text(m)})
    return out


async def summarize_history(llm_call, messages) -> str:
    """把较早消息压缩为摘要；LLM 失败时退化为确定性截断（不抛错）。"""
    if not messages:
        return ""
    lines = []
    for m in messages:
        role = _attr(m, "role", "user")
        who = "助手" if role == "assistant" else "用户"
        lines.append(f"{who}: {message_text(m)}")
    transcript = "\n".join(lines)[:12000]
    prompt = [
        {
            "role": "system",
            "content": (
                "你是对话摘要器。把对话历史压缩为简洁要点，必须保留：用户目标、已确认的结论、"
                "涉及的用例/批次/草稿 id、未决事项。只输出摘要正文（中文）。"
            ),
        },
        {"role": "user", "content": transcript},
    ]
    try:
        text = (await llm_call(prompt) or "").strip()
        if text:
            return text[:4000]
    except Exception:
        pass
    return transcript[-2000:]
