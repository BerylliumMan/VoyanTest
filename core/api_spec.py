# core/api_spec.py
"""api_spec 规范化与校验（029-api-testing）。

契约（specs/029-api-testing/data-model.md §3）：
- ``normalize_api_spec`` 只做**可恢复**的补齐（默认值/排序/序号），
  不可恢复结构问题抛 ``ApiSpecError``；
- ``validate_api_spec`` 返回**人类可读**问题列表（空列表 = 合法），
  供导入/保存/执行前统一把关，不做静默修正。
"""
from __future__ import annotations

from typing import Any

SCHEMA_VERSION = 1

ALLOWED_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}
ALLOWED_BODY_TYPES = {"none", "json", "form", "form_data", "raw", "binary"}
ALLOWED_AUTH_TYPES = {"none", "basic", "bearer", "api_key"}
ALLOWED_ASSERTION_TYPES = {
    "status_code",
    "jsonpath",
    "header",
    "body_contains",
    "body_regex",
    "response_time",
    "jsonschema",
}
ALLOWED_ASSERTION_CONDITIONS = {
    "equals",
    "not_equals",
    "contains",
    "not_contains",
    "gt",
    "gte",
    "lt",
    "lte",
    "exists",
    "not_exists",
    "regex",
}
ALLOWED_EXTRACTOR_TYPES = {"jsonpath", "regex", "header", "cookie"}
ALLOWED_EXTRACTOR_SCOPES = {"case", "environment"}
ALLOWED_FAIL_POLICIES = {"fail_fast", "continue"}
ALLOWED_DATASET_MODES = {"sequential", "random", "loop"}
DEFAULT_TIMEOUT_MS = 30000
MAX_TIMEOUT_MS = 300000


class ApiSpecError(ValueError):
    """api_spec 结构不可恢复问题（无法通过补默认值修复）。"""


def _as_dict(value: Any, field: str) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ApiSpecError(f"{field} 必须是对象（dict），实际为 {type(value).__name__}")
    return value


def _normalize_request(raw: Any, *, step_no: int) -> dict:
    req = _as_dict(raw, f"steps[{step_no}].request")
    method = str(req.get("method") or "GET").strip().upper()
    body = _as_dict(req.get("body"), f"steps[{step_no}].request.body")
    auth = _as_dict(req.get("auth"), f"steps[{step_no}].request.auth")
    timeout = req.get("timeout_ms")
    try:
        timeout = int(timeout) if timeout is not None else DEFAULT_TIMEOUT_MS
    except (TypeError, ValueError):
        timeout = DEFAULT_TIMEOUT_MS
    return {
        **req,
        "method": method,
        "url": str(req.get("url") or ""),
        "headers": list(req.get("headers") or []),
        "query": list(req.get("query") or []),
        "body": {"type": str(body.get("type") or "none"), "content": str(body.get("content") or "")},
        "auth": {**auth, "type": str(auth.get("type") or "none")},
        "timeout_ms": timeout,
        "follow_redirects": bool(req.get("follow_redirects", True)),
        "verify_ssl": bool(req.get("verify_ssl", True)),
    }


def _normalize_step(raw: Any, index: int) -> dict:
    step = _as_dict(raw, f"steps[{index}]")
    if "request" not in step or step.get("request") is None:
        raise ApiSpecError(f"steps[{index}] 缺少 request")
    order = step.get("order")
    try:
        order = int(order) if order is not None else index + 1
    except (TypeError, ValueError):
        order = index + 1
    return {
        **step,
        "order": order,
        "name": str(step.get("name") or ""),
        "enable": bool(step.get("enable", True)),
        "definition_id": step.get("definition_id"),
        "request": _normalize_request(step.get("request"), step_no=index + 1),
        "assertions": list(step.get("assertions") or []),
        "extractors": list(step.get("extractors") or []),
        "pre": list(step.get("pre") or []),
        "post": list(step.get("post") or []),
    }


def normalize_api_spec(spec: Any) -> dict:
    """补齐默认值并排序步骤；不可恢复问题抛 ``ApiSpecError``。"""
    if not isinstance(spec, dict):
        raise ApiSpecError(f"api_spec 必须是对象（dict），实际为 {type(spec).__name__}")
    version = spec.get("schema_version", SCHEMA_VERSION)
    try:
        version = int(version)
    except (TypeError, ValueError):
        raise ApiSpecError(f"schema_version 必须是整数，实际为 {version!r}") from None
    if version != SCHEMA_VERSION:
        raise ApiSpecError(
            f"不支持的 schema_version={version}（当前支持 {SCHEMA_VERSION}）"
        )
    steps_raw = spec.get("steps", [])
    if not isinstance(steps_raw, list):
        raise ApiSpecError(f"steps 必须是数组，实际为 {type(steps_raw).__name__}")
    steps = [_normalize_step(s, i) for i, s in enumerate(steps_raw)]
    steps.sort(key=lambda s: s["order"])
    fail_policy = str(spec.get("fail_policy") or "fail_fast")
    if fail_policy not in ALLOWED_FAIL_POLICIES:
        fail_policy = "fail_fast"
    dataset_mode = str(spec.get("dataset_mode") or "sequential")
    if dataset_mode not in ALLOWED_DATASET_MODES:
        dataset_mode = "sequential"
    return {
        **spec,
        "schema_version": SCHEMA_VERSION,
        "variables": list(spec.get("variables") or []),
        "dataset_id": spec.get("dataset_id"),
        "dataset_mode": dataset_mode,
        "fail_policy": fail_policy,
        "steps": steps,
    }


def validate_api_spec(spec: Any) -> list[str]:
    """返回问题列表（人类可读）；空列表表示合法。"""
    if spec is None:
        return ["api_spec 为空"]
    if not isinstance(spec, dict):
        return [f"api_spec 必须是对象（dict），实际为 {type(spec).__name__}"]
    problems: list[str] = []
    try:
        version = int(spec.get("schema_version", SCHEMA_VERSION))
    except (TypeError, ValueError):
        problems.append(f"schema_version 必须是整数，实际为 {spec.get('schema_version')!r}")
        version = SCHEMA_VERSION
    if version != SCHEMA_VERSION:
        problems.append(f"不支持的 schema_version={version}")
    steps = spec.get("steps")
    if not isinstance(steps, list) or not steps:
        problems.append("steps 必须是非空数组")
        return problems
    fail_policy = spec.get("fail_policy", "fail_fast")
    if fail_policy not in ALLOWED_FAIL_POLICIES:
        problems.append(f"fail_policy 非法: {fail_policy!r}（应为 {'/'.join(sorted(ALLOWED_FAIL_POLICIES))}）")
    dataset_mode = spec.get("dataset_mode", "sequential")
    if dataset_mode not in ALLOWED_DATASET_MODES:
        problems.append(
            f"dataset_mode 非法: {dataset_mode!r}"
            f"（应为 {'/'.join(sorted(ALLOWED_DATASET_MODES))}）"
        )
    for i, raw in enumerate(steps):
        where = f"steps[{i}]"
        if not isinstance(raw, dict):
            problems.append(f"{where} 必须是对象")
            continue
        req = raw.get("request")
        if not isinstance(req, dict):
            problems.append(f"{where}.request 缺失或不是对象")
            continue
        method = str(req.get("method") or "").strip().upper()
        if method not in ALLOWED_METHODS:
            problems.append(f"{where}.request.method 非法: {method!r}")
        if not str(req.get("url") or "").strip():
            problems.append(f"{where}.request.url 不能为空")
        body = req.get("body") or {}
        if isinstance(body, dict):
            btype = str(body.get("type") or "none")
            if btype not in ALLOWED_BODY_TYPES:
                problems.append(f"{where}.request.body.type 非法: {btype!r}")
        auth = req.get("auth") or {}
        if isinstance(auth, dict):
            atype = str(auth.get("type") or "none")
            if atype not in ALLOWED_AUTH_TYPES:
                problems.append(f"{where}.request.auth.type 非法: {atype!r}")
        timeout = req.get("timeout_ms", DEFAULT_TIMEOUT_MS)
        try:
            timeout_i = int(timeout)
        except (TypeError, ValueError):
            problems.append(f"{where}.request.timeout_ms 必须是整数")
            timeout_i = DEFAULT_TIMEOUT_MS
        if not 1000 <= timeout_i <= MAX_TIMEOUT_MS:
            problems.append(
                f"{where}.request.timeout_ms 超出范围（1000~{MAX_TIMEOUT_MS}）: {timeout_i}"
            )
        for j, a in enumerate(raw.get("assertions") or []):
            if not isinstance(a, dict):
                problems.append(f"{where}.assertions[{j}] 必须是对象")
                continue
            atype = str(a.get("type") or "")
            if atype not in ALLOWED_ASSERTION_TYPES:
                problems.append(f"{where}.assertions[{j}].assertion.type 非法: {atype!r}")
            cond = str(a.get("condition") or "equals")
            if cond not in ALLOWED_ASSERTION_CONDITIONS:
                problems.append(f"{where}.assertions[{j}].assertion.condition 非法: {cond!r}")
        for j, e in enumerate(raw.get("extractors") or []):
            if not isinstance(e, dict):
                problems.append(f"{where}.extractors[{j}] 必须是对象")
                continue
            etype = str(e.get("type") or "")
            if etype not in ALLOWED_EXTRACTOR_TYPES:
                problems.append(f"{where}.extractors[{j}].extractor.type 非法: {etype!r}")
            scope = str(e.get("scope") or "case")
            if scope not in ALLOWED_EXTRACTOR_SCOPES:
                problems.append(f"{where}.extractors[{j}].scope 非法: {scope!r}")
            if not str(e.get("variable") or "").strip():
                problems.append(f"{where}.extractors[{j}].variable 不能为空")
    return problems


__all__ = [
    "ApiSpecError",
    "normalize_api_spec",
    "validate_api_spec",
    "SCHEMA_VERSION",
    "ALLOWED_ASSERTION_TYPES",
    "ALLOWED_ASSERTION_CONDITIONS",
    "ALLOWED_EXTRACTOR_TYPES",
    "ALLOWED_EXTRACTOR_SCOPES",
    "ALLOWED_DATASET_MODES",
    "ALLOWED_METHODS",
    "ALLOWED_BODY_TYPES",
    "ALLOWED_AUTH_TYPES",
    "DEFAULT_TIMEOUT_MS",
    "MAX_TIMEOUT_MS",
]
