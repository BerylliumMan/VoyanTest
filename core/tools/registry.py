"""035 工具层：工具注册表（单一事实源）。

- 对话工具循环（core/tools/loop.py）从此发现并调用工具
- MCP 服务端（app/routers/mcp/server.py）从此导出工具清单
- 对话提示词的"可用工具"段落由 describe_markdown() 生成（FR-013）

契约：specs/035-agent-tools-mcp/contracts/agent-tools.md C1/C2。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

SIDE_EFFECTS = ("read", "mutate", "execute")


@dataclass
class ToolContext:
    """一次工具调用的上下文（身份/会话/预算信息）。"""

    db: Any = None
    user_id: Optional[int] = None
    conversation: Any = None           # ChatConversation（对话或影子会话）
    source: str = "chat"               # chat | external
    token_id: Optional[int] = None     # 外部接入令牌 id（chat 来源为 None）
    token_scope: Optional[str] = None  # read | write | None（chat 来源为 None）
    llm: Optional[Callable] = None     # 注入的模型调用（测试/复用）
    extras: dict = field(default_factory=dict)


@dataclass
class ToolSpec:
    """工具定义（C1）。handler 签名：`async def handler(ctx, **args) -> ToolResult`。"""

    name: str
    description: str
    parameters: dict
    side_effect: str
    requires_confirmation: bool
    handler: Callable[..., Awaitable[Any]]
    result_shaper: Optional[Callable] = None

    def __post_init__(self):
        if self.side_effect not in SIDE_EFFECTS:
            raise ValueError(f"非法 side_effect: {self.side_effect}（允许：{SIDE_EFFECTS}）")
        if not isinstance(self.parameters, dict) or self.parameters.get("type") != "object":
            raise ValueError(f"工具 {self.name} 的 parameters 必须是 type=object 的 JSON Schema")


class ToolRegistry:
    """工具注册表（可多实例；生产用模块级 REGISTRY）。"""

    def __init__(self):
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> ToolSpec:
        if spec.name in self._tools:
            raise ValueError(f"工具名重复注册: {spec.name}")
        self._tools[spec.name] = spec
        return spec

    def get(self, name: str) -> Optional[ToolSpec]:
        return self._tools.get(name)

    def all(self) -> list[ToolSpec]:
        return list(self._tools.values())  # 注册顺序稳定

    def names(self) -> list[str]:
        return list(self._tools.keys())

    def clear(self) -> None:  # 测试用
        self._tools.clear()

    # ── 导出：OpenAI tools 格式（C2） ──
    def as_openai_tools(self, allow: Optional[Callable[[ToolSpec], bool]] = None) -> list[dict]:
        out = []
        for spec in self._tools.values():
            if allow and not allow(spec):
                continue
            out.append({
                "type": "function",
                "function": {
                    "name": spec.name,
                    "description": spec.description,
                    "parameters": spec.parameters,
                },
            })
        return out

    # ── 导出：提示词清单（FR-013 单一来源） ──
    def describe_markdown(self, allow: Optional[Callable[[ToolSpec], bool]] = None) -> str:
        lines = []
        for spec in self._tools.values():
            if allow and not allow(spec):
                continue
            props = spec.parameters.get("properties", {})
            required = set(spec.parameters.get("required", []))
            pbits = []
            for pname, pschema in props.items():
                ptype = pschema.get("type", "any")
                flag = "必填" if pname in required else "可选"
                pbits.append(f"{pname}({ptype},{flag})")
            side = {"read": "只读", "mutate": "变更", "execute": "执行"}[spec.side_effect]
            conf = "，需确认" if spec.requires_confirmation else ""
            line = f"- `{spec.name}`（{side}{conf}）：{spec.description}"
            if pbits:
                line += f"；参数：{'、'.join(pbits)}"
            lines.append(line)
        return "\n".join(lines)


REGISTRY = ToolRegistry()


def register_tool(*, name: str, description: str, parameters: dict, side_effect: str = "read",
                  requires_confirmation: bool = False,
                  result_shaper: Optional[Callable] = None):
    """装饰器：注册工具 handler（`async def handler(ctx, **args) -> ToolResult`）。"""

    def deco(fn):
        REGISTRY.register(ToolSpec(
            name=name,
            description=description,
            parameters=parameters,
            side_effect=side_effect,
            requires_confirmation=requires_confirmation,
            handler=fn,
            result_shaper=result_shaper,
        ))
        return fn

    return deco
