"""044 US5：只读隔离子代理。

子代理使用独立 messages 与收窄后的工具注册表；父上下文不继承，
只传 prompt 与只读白名单。结果以最终文本回传主对话。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from core.tools.registry import REGISTRY, ToolRegistry

SUBAGENT_PROMPT = """你是主对话派出的只读子代理，负责检索与核实，不修改任何数据。

规则：
- 只能使用只读工具；没有写操作权限。
- 最终回复直接给出结论与依据（引用的 id、标题、状态），不要复述过程。
- 找不到时明确说明"未找到"，不要臆造。
- 回复用中文，简洁。"""


@dataclass
class SubagentResult:
    status: str
    content: str
    tool_uses: int = 0
    duration_ms: int = 0


def _narrow_registry(source: ToolRegistry, allowlist: list[str] | None) -> ToolRegistry:
    out = ToolRegistry()
    for spec in source.all():
        if spec.side_effect != "read":
            continue
        if spec.name == "agent":  # 防止子代理再派生子代理
            continue
        if allowlist is not None and spec.name not in allowlist:
            continue
        out.register(spec)
    return out


async def run_subagent(
    *,
    ctx,
    prompt: str,
    agent_type: str = "explore",
    max_rounds: int = 100,
    tool_allowlist: list[str] | None = None,
    llm=None,
    registry: ToolRegistry | None = None,
    timeout_s: int = 120,
) -> SubagentResult:
    """在隔离上下文中执行只读子任务。"""
    from core.tools.loop import run_tool_loop

    source = registry if registry is not None else REGISTRY

    if tool_allowlist is not None:
        for name in tool_allowlist:
            spec = source.get(name)
            if spec is None or spec.side_effect != "read":
                return SubagentResult(
                    status="rejected",
                    content=f"子代理只能使用只读工具：{name} 不可用或不是只读工具。",
                )

    sub_registry = _narrow_registry(source, tool_allowlist)
    if not sub_registry.all():
        return SubagentResult(status="rejected", content="子代理没有可用的只读工具。")

    messages = [
        {"role": "system", "content": SUBAGENT_PROMPT},
        {"role": "user", "content": prompt},
    ]
    started = time.monotonic()
    if llm is None:
        llm = getattr(ctx, "llm", None)
    outcome = await run_tool_loop(
        db=getattr(ctx, "db", None),
        conversation=getattr(ctx, "conversation", None),
        ctx=ctx,
        messages=messages,
        llm=llm,
        registry=sub_registry,
        max_rounds=max_rounds,
        timeout_s=timeout_s,
    )
    duration = int((time.monotonic() - started) * 1000)
    status = "max_rounds" if "预算内完成" in (outcome.reply or "") else "completed"
    return SubagentResult(
        status=status,
        content=outcome.reply or "",
        tool_uses=len(outcome.actions_log),
        duration_ms=duration,
    )
