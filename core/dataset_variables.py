# core/dataset_variables.py — UI 用例参数占位符（039）
#
# 语法：{{name}}（与接口测试一致）。提供：
# - discover_parameters: 扫描步骤中的占位符（description/parsed_result/structured_step）
# - validate_parameters: 未声明/拼写提示
# - substitute_text / render_steps: 执行期渲染（不修改原始步骤）
# - missing_columns: 数据集列是否覆盖步骤引用
from __future__ import annotations

import re
from typing import Any, Iterable, Optional

PARAM_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


def _as_dict(step: Any) -> dict:
    if isinstance(step, dict):
        return step
    # ORM TestStep
    return {
        "step_order": getattr(step, "step_order", None),
        "description": getattr(step, "description", None),
        "parsed_result": getattr(step, "parsed_result", None),
        "structured_step": getattr(step, "structured_step", None),
    }


def _iter_strings(value: Any, *, key: Optional[str] = None) -> Iterable[tuple[Optional[str], str]]:
    if isinstance(value, str):
        yield key, value
    elif isinstance(value, dict):
        for k, v in value.items():
            yield from _iter_strings(v, key=str(k))
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _iter_strings(v, key=key)


def discover_parameters(steps: Iterable[Any]) -> list[str]:
    """按首次出现顺序返回步骤中引用的参数名。"""
    seen: dict[str, None] = {}
    for step in steps or []:
        data = _as_dict(step)
        for _, text in _iter_strings(
            {
                "description": data.get("description"),
                "parsed_result": data.get("parsed_result"),
                "structured_step": data.get("structured_step"),
            }
        ):
            for name in PARAM_RE.findall(text):
                seen.setdefault(name, None)
    return list(seen)


def declared_parameter_names(parameters: Optional[list]) -> list[str]:
    names: list[str] = []
    for item in parameters or []:
        if isinstance(item, dict):
            name = str(item.get("name") or "").strip()
        else:
            name = str(item or "").strip()
        if name and name not in names:
            names.append(name)
    return names


def parameter_names(parameters: Optional[list], steps: Iterable[Any]) -> list[str]:
    """声明 + 发现的有序并集（声明优先）。"""
    out = declared_parameter_names(parameters)
    for name in discover_parameters(steps):
        if name not in out:
            out.append(name)
    return out


def validate_parameters(steps: Iterable[Any], declared: Optional[list]) -> list[str]:
    """返回可读错误列表（空 = 通过）。

    - 步骤引用了未声明的参数 → 错误（提示拼写/补充声明）
    - 声明了但未被引用 → 不报错（允许保留说明）
    """
    declared_names = set(declared_parameter_names(declared))
    errors: list[str] = []
    for step in steps or []:
        data = _as_dict(step)
        order = data.get("step_order")
        for _, text in _iter_strings(
            {
                "description": data.get("description"),
                "parsed_result": data.get("parsed_result"),
                "structured_step": data.get("structured_step"),
            }
        ):
            for name in PARAM_RE.findall(text):
                if name not in declared_names:
                    where = f"步骤 {order}" if order is not None else "步骤"
                    msg = f"参数 {name} 未声明（{where}），请检查拼写或补充声明"
                    if msg not in errors:
                        errors.append(msg)
    return errors


def substitute_text(text: Optional[str], values: dict[str, Any], *, strict: bool = False) -> Optional[str]:
    """把 {{name}} 替换为值；strict=True 时缺失值抛 KeyError。"""
    if text is None:
        return None
    if not isinstance(text, str):
        return text

    def repl(match: re.Match) -> str:
        name = match.group(1)
        if name in values:
            value = values[name]
            return "" if value is None else str(value)
        if strict:
            raise KeyError(name)
        return match.group(0)

    return PARAM_RE.sub(repl, text)


def substitute_value(value: Any, values: dict[str, Any], *, strict: bool = False) -> Any:
    """递归替换 dict/list 中的字符串（结构化字段）。"""
    if isinstance(value, str):
        return substitute_text(value, values, strict=strict)
    if isinstance(value, dict):
        return {k: substitute_value(v, values, strict=strict) for k, v in value.items()}
    if isinstance(value, list):
        return [substitute_value(v, values, strict=strict) for v in value]
    return value


def render_step(step: Any, values: dict[str, Any], *, strict: bool = False) -> dict:
    """返回渲染后的步骤字典（不修改原对象）。"""
    data = _as_dict(step)
    return {
        **data,
        "description": substitute_text(data.get("description"), values, strict=strict),
        "parsed_result": substitute_text(data.get("parsed_result"), values, strict=strict),
        "structured_step": substitute_value(
            data.get("structured_step"), values, strict=strict
        ),
    }


def render_steps(steps: Iterable[Any], values: dict[str, Any], *, strict: bool = False) -> list[dict]:
    return [render_step(step, values, strict=strict) for step in steps or []]


def missing_columns(steps: Iterable[Any], columns: Iterable[str]) -> list[str]:
    """数据集的列未覆盖步骤引用的参数 → 返回缺失列名（按首次出现）。"""
    available = {str(c) for c in columns or []}
    out: list[str] = []
    for name in discover_parameters(steps):
        if name not in available and name not in out:
            out.append(name)
    return out


__all__ = [
    "PARAM_RE",
    "declared_parameter_names",
    "discover_parameters",
    "missing_columns",
    "parameter_names",
    "render_step",
    "render_steps",
    "substitute_text",
    "substitute_value",
    "validate_parameters",
]
