# core/api_runner/assertions.py
"""接口测试断言引擎（029-api-testing 契约核心）。

契约（specs/029-api-testing/data-model.md §3 + contracts/api-test-contract.md §2.3）：
1. 断言类型：``status_code`` / ``jsonpath`` / ``header`` / ``body_contains`` /
   ``body_regex`` / ``response_time`` / ``jsonschema``
2. 比较条件：``equals`` / ``not_equals`` / ``contains`` / ``not_contains`` /
   ``gt`` / ``gte`` / ``lt`` / ``lte`` / ``exists`` / ``not_exists`` / ``regex``
3. 同一步的多条断言全部执行（不短路）；``enable=False`` 跳过（不产生结果）
4. 数值比较优先按数字解析（``"200"`` 与 ``200`` 等价），失败则按字符串比较；
   ``gt/gte/lt/lte`` 遇非数字 → 该条 failed 且 error 说明「期望数值」
5. ``error`` 必须含期望/实际；JSONPath 在非 JSON 响应上给出明确原因
6. 单条断言的任何内部错误必须被捕获并转成该条 failed + error，
   ``run_assertions`` 永不因数据形状抛错

调用方：ApiRunner / debug 端点 / 报表组装。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from jsonpath_ng.ext import parse as _jsonpath_parse

try:
    import jsonschema
except ImportError:  # pragma: no cover - 运行环境已安装，防御性兜底
    jsonschema = None  # type: ignore[assignment]

_BODY_PREVIEW_LEN = 200


@dataclass
class ResponseLike:
    """断言的响应载体（纯数据，无网络/IO）。"""

    status: int = 0
    headers: dict = field(default_factory=dict)
    body_text: str = ""
    duration_ms: int = 0


@dataclass
class AssertionResult:
    """单条断言的执行结果。"""

    name: str = ""
    type: str = ""
    condition: str = ""
    expected: str = ""
    actual: str = ""
    passed: bool = False
    error: str = ""


# ── 数值/字符串比较工具 ────────────────────────────────────────────────────

def _to_number(value: Any) -> Optional[float]:
    """尽力把值解析为数字；bool 与无法解析的值返回 None。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _equals(a: Any, b: Any) -> bool:
    """数值优先比较（'200' 与 200 等价），失败则按字符串比较。"""
    na, nb = _to_number(a), _to_number(b)
    if na is not None and nb is not None:
        return na == nb
    return str(a) == str(b)


def _find_header(headers: Any, name: Any) -> Optional[str]:
    """响应头查找（大小写不敏感）。"""
    if not isinstance(headers, dict):
        return None
    target = str(name or "").lower()
    for key, value in headers.items():
        if str(key).lower() == target:
            return value
    return None


# ── 条件求值 ───────────────────────────────────────────────────────────────

def _apply_condition(condition: str, actual: Any, expected: Any) -> tuple[bool, str]:
    """对 (actual, expected) 应用条件，返回 (passed, error)。"""
    if condition == "exists":
        if actual is not None:
            return True, ""
        return False, "期望值存在，实际为 None"
    if condition == "not_exists":
        if actual is None:
            return True, ""
        return False, f"期望值不存在，实际为 {actual!r}"
    if actual is None:
        return False, f"期望 {condition} {expected}，实际为 None"

    if condition == "equals":
        ok = _equals(actual, expected)
        return ok, "" if ok else f"期望 equals {expected}，实际为 {actual}"
    if condition == "not_equals":
        ok = not _equals(actual, expected)
        return ok, "" if ok else f"期望 not_equals {expected}，实际为 {actual}"
    if condition == "contains":
        ok = str(expected) in str(actual)
        return ok, "" if ok else f"期望 contains {expected}，实际为 {actual}"
    if condition == "not_contains":
        ok = str(expected) not in str(actual)
        return ok, "" if ok else f"期望 not_contains {expected}，实际为 {actual}"
    if condition in ("gt", "gte", "lt", "lte"):
        na, nb = _to_number(actual), _to_number(expected)
        if na is None or nb is None:
            return False, (
                f"期望数值比较（{condition}），实际 {actual!r} 与期望 "
                f"{expected!r} 无法解析为数字"
            )
        if condition == "gt":
            ok = na > nb
        elif condition == "gte":
            ok = na >= nb
        elif condition == "lt":
            ok = na < nb
        else:
            ok = na <= nb
        return ok, "" if ok else f"期望 {condition} {expected}，实际为 {actual}"
    if condition == "regex":
        try:
            ok = re.search(str(expected), str(actual)) is not None
        except re.error as exc:
            return False, f"正则无效: {exc}"
        return ok, "" if ok else f"期望 regex {expected}，实际为 {actual}"
    return False, f"未知条件: {condition}"


# ── 各类型取值 ─────────────────────────────────────────────────────────────

def _jsonpath_value(expression: Any, body_text: str) -> tuple[Any, str]:
    """JSONPath 取值：命中多值取第一个；无命中返回 (None, '')。"""
    text = body_text or ""
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        preview = text[:_BODY_PREVIEW_LEN]
        return None, f"响应不是合法 JSON（前 {_BODY_PREVIEW_LEN} 字符: {preview}）"
    try:
        matches = _jsonpath_parse(str(expression or "")).find(data)
    except Exception as exc:  # 非法表达式等
        return None, f"JSONPath 表达式无效: {expression!r}（{exc}）"
    if not matches:
        return None, ""
    return matches[0].value, ""


def _jsonschema_check(
    expression: Any, expected: Any, response: ResponseLike
) -> tuple[bool, str, str]:
    """JSON Schema 校验：expression 为空时校验整个 body。"""
    if jsonschema is None:  # pragma: no cover
        return False, "jsonschema 库不可用", ""
    schema_text = str(expression or expected or "")
    try:
        schema = json.loads(schema_text)
    except (ValueError, TypeError):
        return False, f"JSON Schema 文本无效: {schema_text[:_BODY_PREVIEW_LEN]!r}", ""
    try:
        data = json.loads(response.body_text or "")
    except (ValueError, TypeError):
        preview = (response.body_text or "")[:_BODY_PREVIEW_LEN]
        return False, f"响应不是合法 JSON（前 {_BODY_PREVIEW_LEN} 字符: {preview}）", ""
    try:
        jsonschema.validate(data, schema)
        return True, "", "通过"
    except jsonschema.ValidationError as exc:
        path = "/".join(str(p) for p in exc.path) or "/"
        return False, f"JSON Schema 校验失败: {exc.message}（路径: {path}）", ""
    except jsonschema.SchemaError as exc:
        return False, f"JSON Schema 定义无效: {exc.message}", ""


def _extract_actual(
    atype: str, expression: Any, response: ResponseLike
) -> tuple[Any, str]:
    """按断言类型从响应取值，返回 (actual, error)。"""
    if atype == "status_code":
        return str(response.status), ""
    if atype == "header":
        return _find_header(response.headers, expression), ""
    if atype == "body_contains":
        return response.body_text or "", ""
    if atype == "body_regex":
        return response.body_text or "", ""
    if atype == "response_time":
        return str(response.duration_ms), ""
    if atype == "jsonpath":
        return _jsonpath_value(expression, response.body_text)
    return None, f"未知断言类型: {atype}"


# ── 单条断言执行 ───────────────────────────────────────────────────────────

def _run_one(assertion: Any, response: ResponseLike) -> Optional[AssertionResult]:
    """执行单条断言；``enable=False`` 返回 None（跳过，不产生结果）。"""
    if not isinstance(assertion, dict):
        return AssertionResult(
            name="", type="", condition="", expected="", actual="",
            passed=False, error="断言条目格式错误（应为对象）",
        )
    if assertion.get("enable") is False:
        return None
    name = str(assertion.get("name") or "")
    atype = str(assertion.get("type") or "")
    condition = str(assertion.get("condition") or "equals")
    expected = assertion.get("expected")
    expression = assertion.get("expression")
    expected_str = "" if expected is None else str(expected)

    try:
        if atype == "jsonschema":
            passed, error, actual = _jsonschema_check(expression, expected, response)
            return AssertionResult(
                name=name, type=atype, condition=condition,
                expected=expected_str, actual=actual, passed=passed, error=error,
            )
        actual, extract_error = _extract_actual(atype, expression, response)
        if extract_error:
            return AssertionResult(
                name=name, type=atype, condition=condition,
                expected=expected_str, actual="", passed=False, error=extract_error,
            )
        passed, cond_error = _apply_condition(condition, actual, expected)
        # 未命中（actual 为 None）时补充可读的未命中上下文
        if cond_error and actual is None and expression not in (None, ""):
            cond_error += f"（未命中: {expression}）"
        return AssertionResult(
            name=name, type=atype, condition=condition,
            expected=expected_str,
            actual="" if actual is None else str(actual),
            passed=passed, error=cond_error,
        )
    except Exception as exc:  # 兜底：任何内部错误转成该条 failed
        return AssertionResult(
            name=name, type=atype, condition=condition,
            expected=expected_str, actual="", passed=False,
            error=f"断言执行异常: {exc}",
        )


def run_assertions(
    assertions: list[dict], response: ResponseLike
) -> list[AssertionResult]:
    """执行全部断言（不短路），返回结果列表；``enable=False`` 的条目跳过。"""
    results: list[AssertionResult] = []
    for assertion in assertions or []:
        result = _run_one(assertion, response)
        if result is not None:
            results.append(result)
    return results


__all__ = [
    "ResponseLike",
    "AssertionResult",
    "run_assertions",
]