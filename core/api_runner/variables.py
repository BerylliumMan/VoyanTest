# core/api_runner/variables.py
"""变量渲染与脱敏（029-api-testing 契约核心）。

契约（specs/029-api-testing/contracts/api-test-contract.md §3）：
1. 语法 ``{{name}}``；未闭合的 ``{{`` 视为普通文本（不报错）
2. 内置变量 ``{{$uuid}}`` / ``{{$timestamp}}`` / ``{{$timestampMs}}`` /
   ``{{$randomInt}}`` / ``{{$date}}``
3. 查找顺序（高 → 低）：runtime（提取器产出） > step > case > dataset > env > baseUrl
4. 变量值递归渲染深度 ≤ 5，超限报错（防循环引用）
5. 未定义变量 → ``UndefinedVariableError``（列出缺失名 + 可用名），阻止发送
6. 脱敏：``mask_value`` / ``mask_headers`` 在写日志、报告、UI 回显前必须调用

调用方：runner / debug 端点 / 报表组装 / 前端回显。
"""
from __future__ import annotations

import random
import re
import uuid
from datetime import date
from typing import Any, Iterable, Optional

MAX_RENDER_DEPTH = 5
_MASK_KEEP = 6
_VAR_RE = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}")
_SECRET_NAME_SUBSTRINGS = (
    "authorization",
    "cookie",
    "x-api-key",
    "api-key",
    "token",
    "secret",
    "password",
)


class VariableError(ValueError):
    """变量渲染相关的所有失败（父类）。"""


class UndefinedVariableError(VariableError):
    """未定义变量：``missing`` 为缺失名列表，``available`` 为可用名列表。"""

    def __init__(self, missing: Iterable[str], available: Iterable[str] = ()): 
        self.missing = sorted({str(m) for m in missing})
        self.available = sorted({str(a) for a in available})
        preview = ", ".join(self.available[:30]) + ("…" if len(self.available) > 30 else "")
        super().__init__(
            f"未定义变量: {self.missing}；可用变量: [{preview}]"
        )


def _now_ts() -> float:
    import time

    return time.time()


def _builtin(name: str) -> Optional[str]:
    if not name.startswith("$"):
        return None
    if name == "$uuid":
        return str(uuid.uuid4())
    if name == "$timestamp":
        return str(int(_now_ts()))
    if name == "$timestampMs":
        return str(int(_now_ts() * 1000))
    if name == "$randomInt":
        return str(random.randint(0, 999999))
    if name == "$date":
        return date.today().isoformat()
    return None


class VariableScope:
    """六级变量作用域（高 → 低：runtime > step > case > dataset > env > baseUrl）。"""

    def __init__(
        self,
        *,
        runtime: Optional[dict] = None,
        step: Optional[dict] = None,
        case_variables: Optional[dict] = None,
        dataset_row: Optional[dict] = None,
        env_variables: Optional[dict] = None,
        base_url: str = "",
    ) -> None:
        self.runtime: dict[str, str] = dict(runtime or {})
        self.step: dict[str, str] = dict(step or {})
        self.case: dict[str, str] = dict(case_variables or {})
        self.dataset: dict[str, str] = dict(dataset_row or {})
        self.env: dict[str, str] = dict(env_variables or {})
        self.base_url = str(base_url or "")
        # 提取器 scope="environment" 写入的变量（区别于环境配置自带变量）
        self.env_extracted: dict[str, str] = {}

    # ── 查询/写入 ───────────────────────────────────────────────────────────
    def lookup(self, name: str) -> Optional[str]:
        for layer in (self.runtime, self.step, self.case, self.dataset, self.env):
            if name in layer:
                return layer[name]
        if name in ("baseUrl", "base_url", "BASE_URL") and self.base_url:
            return self.base_url
        return None

    def available_names(self) -> list[str]:
        names: set[str] = set()
        for layer in (self.runtime, self.step, self.case, self.dataset, self.env):
            names |= set(layer)
        if self.base_url:
            names.add("baseUrl")
        return sorted(names)

    def set_runtime(self, key: str, value: Any) -> None:
        self.runtime[str(key)] = "" if value is None else str(value)

    def set_env(self, key: str, value: Any) -> None:
        self.env[str(key)] = "" if value is None else str(value)
        self.env_extracted[str(key)] = "" if value is None else str(value)


def variables_to_map(items: Optional[Iterable[dict]]) -> dict[str, str]:
    """``[{key,value,enable}]`` → ``{key: value}``，跳过禁用项与空 key。"""
    out: dict[str, str] = {}
    for it in items or []:
        if not isinstance(it, dict):
            continue
        key = str(it.get("key") or "").strip()
        if not key or it.get("enable") is False:
            continue
        value = it.get("value")
        out[key] = "" if value is None else str(value)
    return out


def secret_keys(items: Optional[Iterable[dict]]) -> set[str]:
    """标记为 secret 的变量名集合（用于报告脱敏）。"""
    out: set[str] = set()
    for it in items or []:
        if isinstance(it, dict) and it.get("secret"):
            key = str(it.get("key") or "").strip()
            if key:
                out.add(key)
    return out


def render(template: Any, scope: VariableScope, _depth: int = 0) -> str:
    """渲染 ``{{var}}`` 模板；未定义变量抛 ``UndefinedVariableError``。"""
    if template is None:
        return ""
    text = str(template)
    if _depth > MAX_RENDER_DEPTH:
        raise VariableError(
            f"变量渲染深度超限（>{MAX_RENDER_DEPTH}），可能存在循环引用: {text[:120]!r}"
        )
    if "{{" not in text:
        return text

    missing: list[str] = []
    replaced = False

    def _sub(match: re.Match) -> str:
        nonlocal replaced
        name = match.group(1).strip()
        builtin = _builtin(name)
        if builtin is not None:
            replaced = True
            return builtin
        value = scope.lookup(name)
        if value is None:
            missing.append(name)
            return match.group(0)
        replaced = True
        return value

    rendered = _VAR_RE.sub(_sub, text)
    if missing:
        raise UndefinedVariableError(missing, scope.available_names())
    # 变量值里可能再含 {{...}}：递归渲染（深度受限）。
    # 只有确实发生过替换才递归——未闭合的 "{{" 必须原样返回（契约 §3.1）。
    if replaced and "{{" in rendered:
        return render(rendered, scope, _depth + 1)
    return rendered


def is_secret_name(name: Any) -> bool:
    """名字是否暗示敏感值（请求头名 / 提取变量名共用一套判定）。

    用于两个必须一致的场景：写报告时决定哪些头要打码、哪些提取值要打码。
    """
    lowered = str(name or "").lower()
    return any(hint in lowered for hint in _SECRET_NAME_SUBSTRINGS)


def mask_value(value: Any, keep: int = _MASK_KEEP) -> str:
    """脱敏单个值：保留前 ``keep`` 位，其余折叠为 ``***``。"""
    if value is None:
        return ""
    text = str(value)
    if not text:
        return ""
    if len(text) <= keep:
        return "***"
    return f"{text[:keep]}***"


def _is_secret_header(key: str) -> bool:
    lowered = (key or "").lower()
    return is_secret_name(key)


def mask_headers(headers: Any) -> Any:
    """脱敏请求/响应头（保持原结构：dict 或 ``[{key,value}]`` 列表）。"""
    if isinstance(headers, dict):
        return {
            k: (mask_value(v) if _is_secret_header(str(k)) else v)
            for k, v in headers.items()
        }
    if isinstance(headers, list):
        out = []
        for h in headers:
            if isinstance(h, dict):
                key = str(h.get("key") or "")
                if _is_secret_header(key):
                    out.append({**h, "value": mask_value(h.get("value"))})
                else:
                    out.append(dict(h))
            else:
                out.append(h)
        return out
    return headers


def mask_secrets_in_text(text: Any, secrets: Iterable[str]) -> str:
    """把文本里出现的 secret 变量值替换为脱敏形式（用于请求体/响应体落库）。"""
    out = "" if text is None else str(text)
    for secret in secrets:
        if secret and len(str(secret)) >= 4:
            out = out.replace(str(secret), mask_value(secret))
    return out


__all__ = [
    "MAX_RENDER_DEPTH",
    "VariableError",
    "UndefinedVariableError",
    "VariableScope",
    "variables_to_map",
    "secret_keys",
    "render",
    "mask_value",
    "is_secret_name",
    "mask_headers",
    "mask_secrets_in_text",
]
