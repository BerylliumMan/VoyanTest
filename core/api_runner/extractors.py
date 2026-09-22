# core/api_runner/extractors.py
"""响应提取器（029-api-testing）。

契约（specs/029-api-testing/data-model.md §3/§4 + contracts §2.4）：
1. 四种类型：``jsonpath`` / ``regex`` / ``header`` / ``cookie``
2. 命中取值规则：JSONPath 命中多值取第一个；正则优先取第 1 组、无组取整段；
   响应头大小写不敏感、多值取第一个；Cookie 从 ``Set-Cookie``（支持多行）解析
3. ``required=True``（默认）未命中 → 抛 ``ExtractionError``（携带 variable /
   expression / reason / snippet / 已提取变量），阻止后续步骤使用残缺变量
4. ``required=False`` 未命中 → 记 ``ok=False`` 结果，不写入变量字典
5. ``enable=False`` 条目完全跳过（不产生结果）；变量名为空 → 记错误结果
6. ``scope``（case|environment）原样回传，由调用方决定写入哪一层作用域
7. 纯函数、无网络、无 IO；单条内部异常不得炸整步

调用方：core/api_runner/runner.py、调试端点。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from core.api_runner.assertions import ResponseLike

_SNIPPET_LEN = 200
ALLOWED_TYPES = ("jsonpath", "regex", "header", "cookie")


class ExtractionError(ValueError):
    """``required=True`` 的提取失败（携带定位信息与已提取变量）。"""

    def __init__(
        self,
        *,
        variable: str,
        expression: str,
        reason: str,
        snippet: str = "",
        extracted: Optional[dict] = None,
    ) -> None:
        self.variable = variable
        self.expression = expression
        self.reason = reason
        self.snippet = (snippet or "")[:_SNIPPET_LEN]
        self.extracted = dict(extracted or {})
        super().__init__(f"提取失败 [{variable}]: {reason}（表达式: {expression}）")


@dataclass
class ExtractorResult:
    """单条提取器的执行结果（供报告展示）。"""

    variable: str = ""
    value: str = ""
    scope: str = "case"
    type: str = ""
    expression: str = ""
    ok: bool = False
    error: str = ""


def _json_body(body_text: str) -> tuple[Optional[Any], str]:
    try:
        return json.loads(body_text or ""), ""
    except Exception:
        preview = (body_text or "")[:_SNIPPET_LEN]
        return None, f"响应不是合法 JSON（前 {_SNIPPET_LEN} 字符: {preview}）"


def _to_variable_value(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def _header_value(headers: Any, name: str) -> Optional[str]:
    if not isinstance(headers, dict):
        return None
    target = (name or "").lower()
    for key, value in headers.items():
        if str(key).lower() != target:
            continue
        if isinstance(value, (list, tuple)):
            return str(value[0]) if value else None
        return str(value)
    return None


def _set_cookie_values(headers: Any) -> list[str]:
    if not isinstance(headers, dict):
        return []
    out: list[str] = []
    for key, value in headers.items():
        if str(key).lower() != "set-cookie":
            continue
        if isinstance(value, (list, tuple)):
            out.extend(str(v) for v in value)
        else:
            out.append(str(value))
    return out


def _cookie_value(headers: Any, name: str) -> Optional[str]:
    for raw in _set_cookie_values(headers):
        for part in raw.split(";"):
            chunk = part.strip()
            if not chunk or "=" not in chunk:
                continue
            key, _, value = chunk.partition("=")
            if key.strip() == name:
                return value.strip()
    return None


def _extract_jsonpath(expression: str, response: ResponseLike) -> tuple[bool, str, str]:
    from jsonpath_ng.ext import parse

    body, body_error = _json_body(response.body_text)
    if body_error:
        return False, "", body_error
    try:
        compiled = parse(expression)
    except Exception as exc:
        return False, "", f"JSONPath 表达式无效: {expression!r}（{exc}）"
    matches = compiled.find(body)
    if not matches:
        return False, "", f"JSONPath 无匹配: {expression}"
    return True, _to_variable_value(matches[0].value), ""


def _extract_regex(expression: str, response: ResponseLike) -> tuple[bool, str, str]:
    try:
        pattern = re.compile(expression)
    except re.error as exc:
        return False, "", f"正则表达式无效: {expression!r}（{exc}）"
    match = pattern.search(response.body_text or "")
    if not match:
        return False, "", f"正则无匹配: {expression}"
    value = match.group(1) if pattern.groups else match.group(0)
    return True, value, ""


def _extract_header(expression: str, response: ResponseLike) -> tuple[bool, str, str]:
    value = _header_value(response.headers, expression)
    if value is None:
        return False, "", f"响应头不存在: {expression}"
    return True, value, ""


def _extract_cookie(expression: str, response: ResponseLike) -> tuple[bool, str, str]:
    value = _cookie_value(response.headers, expression)
    if value is None:
        return False, "", f"Cookie 不存在: {expression}"
    return True, value, ""


_EXTRACTORS = {
    "jsonpath": _extract_jsonpath,
    "regex": _extract_regex,
    "header": _extract_header,
    "cookie": _extract_cookie,
}


def _run_one(etype: str, expression: str, response: ResponseLike) -> tuple[bool, str, str]:
    handler = _EXTRACTORS.get(etype)
    if handler is None:
        return False, "", f"未知的提取类型: {etype!r}（支持 {', '.join(ALLOWED_TYPES)}）"
    return handler(expression, response)


def run_extractors(
    extractors: Optional[Iterable[dict]], response: ResponseLike
) -> tuple[dict[str, str], list[ExtractorResult]]:
    """执行提取器列表，返回 ``(变量字典, 逐条结果)``。"""
    variables: dict[str, str] = {}
    results: list[ExtractorResult] = []
    for entry in extractors or []:
        if not isinstance(entry, dict) or entry.get("enable") is False:
            continue
        variable = str(entry.get("variable") or "").strip()
        etype = str(entry.get("type") or "")
        expression = str(entry.get("expression") or "")
        scope = str(entry.get("scope") or "case")
        required = entry.get("required", True) is not False
        base = {
            "variable": variable,
            "scope": scope,
            "type": etype,
            "expression": expression,
        }
        if not variable:
            results.append(
                ExtractorResult(**base, ok=False, error="变量名（variable）不能为空")
            )
            continue
        try:
            ok, value, reason = _run_one(etype, expression, response)
        except Exception as exc:  # 防御：提取器内部异常不得炸整步
            ok, value, reason = False, "", f"提取异常: {exc}"
        if ok:
            variables[variable] = value
            results.append(ExtractorResult(**base, value=value, ok=True))
            continue
        if required:
            raise ExtractionError(
                variable=variable,
                expression=expression,
                reason=reason or "未命中",
                snippet=response.body_text,
                extracted=variables,
            )
        results.append(ExtractorResult(**base, ok=False, error=reason or "未命中"))
    return variables, results


__all__ = [
    "ALLOWED_TYPES",
    "ExtractionError",
    "ExtractorResult",
    "ResponseLike",
    "run_extractors",
]
