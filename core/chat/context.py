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
            text = str(r.get("text") or "").strip()
            if text:
                lines.append(f"【文件内容（节选）】\n{text}")
            elif r.get("text_error"):
                lines.append(f"（文件未能提取内容：{r.get('text_error')}）")
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
            text = str(r.get("text") or "").strip()
            if text:
                lines.append(f"【文件内容（节选）】\n{text}")
            elif r.get("text_error"):
                lines.append(f"（文件未能提取内容：{r.get('text_error')}）")
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


def build_suites_block(suites) -> str:
    """项目用例集清单（对话内执行用例集用；仅渲染 id/名称/类型/条数）。"""
    lines = []
    for s in suites or []:
        if not isinstance(s, dict):
            continue
        try:
            sid = int(s.get("id"))
        except (TypeError, ValueError):
            continue
        name = str(s.get("name") or "").strip() or f"用例集#{sid}"
        kind = "UI" if (s.get("case_kind") or "ui") == "ui" else "功能"
        count = s.get("count")
        suffix = f"（{kind}" + (f"，{count} 条）" if count is not None else "）")
        lines.append(f"- 用例集#{sid} {name}{suffix}")
    if not lines:
        return ""
    return "【项目用例集 — 执行用例集时 suite_id 只能取自本列表】\n" + "\n".join(lines)


def build_llm_messages(
    *,
    system_prompt: str,
    summary: str | None = None,
    messages,
    refs=None,
    project_name: str | None = None,
    draft_digest: str | None = None,
    environments=None,
    suites=None,
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
    suite_block = build_suites_block(suites)
    if suite_block:
        ctx_parts.append(suite_block)
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


def compact_messages(messages: list[dict], max_chars: int) -> list[dict]:
    """最后一道防线（ZCode 式）：装配后的 messages 总字符超预算时压缩较早内容。

    - 增量摘要（summarize_history）覆盖跨轮历史；本函数兜底 system/引用块/最近消息
      叠加后仍超预算的场景（2026-10-19 用户反馈「达到上限没有压缩」）；
    - 保留 system 与最后 2 条不动；从最早的历史消息起逐步截断（首 1200 字符 + 标记），
      仍超标时压缩较近消息（首 3000 字符）；返回新列表（不改原对象）。
    """
    if max_chars <= 0 or not messages:
        return messages

    def _total(items: list[dict]) -> int:
        return sum(len(str(m.get("content") or "")) for m in items)

    if _total(messages) <= max_chars:
        return messages
    out = [dict(m) for m in messages]
    for i in range(1, max(1, len(out) - 2)):
        content = str(out[i].get("content") or "")
        if len(content) <= 1200:
            continue
        out[i]["content"] = content[:1200] + "\n…（上下文自动压缩：该消息较早内容已省略）"
        if _total(out) <= max_chars:
            return out
    for i in range(max(1, len(out) - 2), len(out)):
        content = str(out[i].get("content") or "")
        if len(content) <= 3000:
            continue
        out[i]["content"] = content[:3000] + "\n…（上下文自动压缩：该消息内容已省略）"
        if _total(out) <= max_chars:
            break
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
