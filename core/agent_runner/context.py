# core/agent_runner/context.py
"""Agent 工作记忆 — 滑动窗口压缩 + token 计数。

AgentContext 为 OTA（Observe-Think-Act）循环提供短期记忆：
- 维护最近 N 轮对话历史
- 超出窗口上限时自动压缩旧轮次为摘要
- 提供 token 估算接口，辅助 LLM 调用预算控制
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# ── 常量 ──────────────────────────────────────────────────────────────────────

APPROX_CHARS_PER_TOKEN = 4  # 中英混合平均 ~4 chars/token
COMPRESS_THRESHOLD_RATIO = 0.5  # 保留最近 50% 轮次，压缩更早的


@dataclass
class TurnRecord:
    """单轮对话记录。"""
    role: str
    content: str
    tool_calls: list[dict[str, Any]] | None = None
    tool_result: str | None = None

    def to_text(self) -> str:
        """将本轮记录转换为 LLM 可读文本。"""
        parts = [f"[{self.role}] {self.content}"]
        if self.tool_calls:
            for tc in self.tool_calls:
                action = tc.get("action", "?")
                value = tc.get("value", "")
                parts.append(f"  → tool_call: {action}({value})")
        if self.tool_result:
            parts.append(f"  → result: {self.tool_result}")
        return "\n".join(parts)


# ── AgentContext ──────────────────────────────────────────────────────────────


class AgentContext:
    """Agent 工作记忆 — 滑动窗口压缩 + token 计数。

    在 OTA 循环中，每轮 observe → think → act 产生两条记录：
    - assistant 的 think 输出（工具调用计划）
    - tool 的执行结果

    使用方法::

        ctx = AgentContext(max_turns=10)
        ctx.add_turn("user", "目标：登录系统")
        ctx.add_turn("assistant", "观察到登录页面，尝试点击登录按钮",
                     tool_calls=[{"action": "click", "selector": "e15"}])
        ctx.add_turn("tool", "click 成功")

        prompt = ctx.get_context()  # 压缩后的对话历史
        tokens = ctx.estimate_tokens()  # 约 1234
    """

    def __init__(self, max_turns: int = 10):
        """初始化工作记忆。

        Args:
            max_turns: 窗口内保留的最大轮次数。超出后触发压缩。
        """
        self.turns: list[TurnRecord] = []
        self.max_turns = max_turns
        self.summary: str = ""  # 被压缩轮次的摘要文本

    # ── 添加轮次 ─────────────────────────────────────────────────────────

    def add_turn(
        self,
        role: str,
        content: str,
        tool_calls: list[dict[str, Any]] | None = None,
        tool_result: str | None = None,
    ) -> None:
        """添加一轮对话记录，超出窗口上限时自动压缩。

        Args:
            role: 角色标签（user / assistant / tool / system）
            content: 本轮文本内容
            tool_calls: 本轮产生的工具调用列表（assistant 角色使用）
            tool_result: 工具执行结果文本（tool 角色使用）
        """
        record = TurnRecord(
            role=role,
            content=content,
            tool_calls=tool_calls,
            tool_result=tool_result,
        )
        self.turns.append(record)

        # 超出窗口时触发压缩
        if len(self.turns) > self.max_turns * 2:
            self._compress()

    # ── 获取上下文 ─────────────────────────────────────────────────────────

    def get_context(self) -> str:
        """获取完整上下文文本，供 LLM 调用使用。

        Returns:
            摘要（如有） + 最近 N 轮对话文本
        """
        parts: list[str] = []

        if self.summary:
            parts.append(f"## 历史摘要\n{self.summary}")

        if self.turns:
            parts.append("## 最近对话")
            for turn in self.turns:
                parts.append(turn.to_text())

        return "\n\n".join(parts) if parts else "(空上下文)"

    # ── Token 估算 ─────────────────────────────────────────────────────────

    def estimate_tokens(self) -> int:
        """粗略估算当前上下文的 token 数量。

        Returns:
            估算的 token 数（~4 chars/token）
        """
        total_chars = sum(
            len(t.content) + (len(t.tool_result or "")) for t in self.turns
        )
        total_chars += len(self.summary)
        return max(1, total_chars // APPROX_CHARS_PER_TOKEN)

    # ── 内部压缩 ───────────────────────────────────────────────────────────

    def _compress(self) -> None:
        """将最早的轮次压缩为摘要文本。

        策略：保留最近 max_turns 轮，将更早的轮次用 LLM 压缩为
        简短摘要（当前版本使用简单文本拼接，后续可接入 LLM 摘要）。
        """
        keep_count = self.max_turns
        if keep_count >= len(self.turns):
            return

        # 取最早的轮次生成摘要
        old_turns = self.turns[:-keep_count]
        compressed_parts: list[str] = []
        for t in old_turns:
            short = t.content[:200]
            if len(t.content) > 200:
                short += "..."
            compressed_parts.append(f"[{t.role}] {short}")
            if t.tool_result:
                compressed_parts.append(f"  → {t.tool_result[:100]}")

        new_summary = "\n".join(compressed_parts)
        if self.summary:
            self.summary = f"{self.summary}\n{new_summary}"
        else:
            self.summary = new_summary

        # 保留最近轮次
        self.turns = self.turns[-keep_count:]
        logger.debug(
            "AgentContext 压缩: %d 轮 → 摘要, 保留 %d 轮",
            len(old_turns),
            keep_count,
        )

    # ── 工具方法 ───────────────────────────────────────────────────────────

    def clear(self) -> None:
        """清空所有上下文，包括摘要。"""
        self.turns.clear()
        self.summary = ""

    def to_dict(self) -> dict[str, Any]:
        """导出为可序列化字典（用于持久化/调试）。"""
        return {
            "turns": [
                {
                    "role": t.role,
                    "content": t.content,
                    "tool_calls": t.tool_calls,
                    "tool_result": t.tool_result,
                }
                for t in self.turns
            ],
            "summary": self.summary,
            "max_turns": self.max_turns,
            "estimated_tokens": self.estimate_tokens(),
        }
