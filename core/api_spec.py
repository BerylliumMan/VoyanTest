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
ALLOWED_BODY_TYPES = {"none", "json", "form", "form_data", "raw", "binary", "multipart"}
# 031（US1）：multipart 语义的规范类型是既有 form_data（UI 早已如此映射）；
# "multipart" 仅作兼容别名，归一化时折叠为 "form_data"。
ALLOWED_AUTH_TYPES = {"none", "basic", "bearer", "api_key"}
ALLOWED_ASSERTION_TYPES = {
    # 031（US6）：表达式断言（沙箱求值，结果须为 bool）
    "expression",
    "status_code",
    "jsonpath",
    "header",
    "body_contains",
    "body_regex",
    "response_time",
    "jsonschema",
}
ALLOWED_ASSERTION_CONDITIONS = {
    # 031（US6）：表达式断言的占位 condition（前端固定填 expression）
    "expression",
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
# 031-api-testing-enhancements：重试与脚本（服务端执行）
ALLOWED_RETRY_ON = {"5xx", "timeout", "network"}
MAX_RETRY_COUNT = 3
PLATFORM_FILE_PREFIX = "platform://"
MAX_SCRIPT_CHARS = 20000
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


def _normalize_form_data(raw: Any) -> list[dict]:
    """031（US1）：multipart 普通字段归一化。"""
    out: list[dict] = []
    for item in raw or []:
        if isinstance(item, dict):
            out.append(
                {
                    "key": str(item.get("key") or ""),
                    "value": str(item.get("value") or ""),
                    "enable": bool(item.get("enable", True)),
                }
            )
    return out


def _normalize_files(raw: Any) -> list[dict]:
    """031（US1）：multipart 文件字段归一化（path 为 platform://<id> 或变量引用）。"""
    out: list[dict] = []
    for item in raw or []:
        if isinstance(item, dict):
            out.append(
                {
                    "key": str(item.get("key") or ""),
                    "path": str(item.get("path") or ""),
                    "content_type": item.get("content_type") or None,
                }
            )
    return out


def _normalize_script_timeout(value):
    """脚本超时（200~10000ms）；None/非法 → None（用系统默认）。"""
    if value in (None, ""):
        return None
    try:
        timeout = int(value)
    except (TypeError, ValueError):
        return None
    if not 200 <= timeout <= 10000:
        return None
    return timeout


def _normalize_retry(raw: Any) -> dict | None:
    """031（US5）：重试策略归一化；``None`` 表示走 runner 默认（网络/超时 × 1）。

    ``on`` 缺省 = ["timeout","network"]（5xx 需显式加入，避免掩盖服务端缺陷）。
    """
    if not raw:
        return None
    if not isinstance(raw, dict):
        raw = {}
    try:
        max_retry = int(raw.get("max") or 0)
    except (TypeError, ValueError):
        max_retry = 0
    try:
        delay_ms = int(raw.get("delay_ms") or 300)
    except (TypeError, ValueError):
        delay_ms = 300
    on = [str(x) for x in (raw.get("on") or []) if str(x) in ALLOWED_RETRY_ON]
    if not on:
        on = ["timeout", "network"]
    return {
        "max": max(0, min(max_retry, MAX_RETRY_COUNT)),
        "delay_ms": max(0, delay_ms),
        "on": on,
    }


def _validate_multipart(body: dict, where: str, problems: list[str]) -> None:
    """031（US1）：multipart 结构与文件引用校验。"""
    files = body.get("files") or []
    form_data = body.get("form_data") or []
    if not isinstance(files, list) or not isinstance(form_data, list):
        problems.append(f"{where}.request.body 的 form_data/files 必须是数组")
        return
    if not files and not form_data:
        problems.append(f"{where}.request.body multipart 至少需要 form_data 或 files 之一")
    for j, item in enumerate(files):
        if not isinstance(item, dict):
            problems.append(f"{where}.request.body.files[{j}] 必须是对象")
            continue
        if not str(item.get("key") or "").strip():
            problems.append(f"{where}.request.body.files[{j}].key 不能为空")
        path = str(item.get("path") or "").strip()
        if not path:
            problems.append(f"{where}.request.body.files[{j}].path 不能为空")
        elif path.startswith(PLATFORM_FILE_PREFIX):
            file_id = path[len(PLATFORM_FILE_PREFIX):]
            if not file_id.isdigit():
                problems.append(
                    f"{where}.request.body.files[{j}].path 的 file_id 必须是正整数（实际 {path!r}）"
                )
        elif "{{" not in path:
            problems.append(
                f"{where}.request.body.files[{j}].path 需为 platform://<file_id> 或变量引用"
                f"（实际 {path!r}）"
            )


def _validate_retry(raw: Any, where: str, problems: list[str]) -> None:
    """031（US5）：重试策略校验（越界值在归一化时已收敛，这里只报不可用的输入）。"""
    if not isinstance(raw, dict):
        problems.append(f"{where}.retry 必须是对象")
        return
    try:
        max_retry = int(raw.get("max") or 0)
    except (TypeError, ValueError):
        problems.append(f"{where}.retry.max 必须是整数")
        max_retry = 0
    if max_retry < 0 or max_retry > MAX_RETRY_COUNT:
        problems.append(f"{where}.retry.max 超出范围（0~{MAX_RETRY_COUNT}）: {max_retry}")
    on = raw.get("on")
    if on is not None:
        bad = [str(x) for x in on if str(x) not in ALLOWED_RETRY_ON]
        if bad:
            problems.append(
                f"{where}.retry.on 含非法触发条件 {bad}（应为 {'/'.join(sorted(ALLOWED_RETRY_ON))}）"
            )


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
        "body": {
            # 031：兼容别名 multipart → 规范值 form_data
            "type": (
                "form_data"
                if str(body.get("type") or "none") == "multipart"
                else str(body.get("type") or "none")
            ),
            "content": str(body.get("content") or ""),
            # 031（US1）：multipart 的普通字段与文件字段（非 multipart 时保持空数组，零影响）
            "form_data": _normalize_form_data(body.get("form_data")),
            "files": _normalize_files(body.get("files")),
        },
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
        # 031：脚本与重试（默认空/None → runner 走既有行为 + 默认重试策略）
        "pre_script": str(step.get("pre_script") or ""),
        "post_script": str(step.get("post_script") or ""),
        # 031（US3）：脚本超时步骤级覆盖（None → 用 app/config.py 默认）
        "script_timeout_ms": _normalize_script_timeout(step.get("script_timeout_ms")),
        "retry": _normalize_retry(step.get("retry")),
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
    # 031（US9）：loop 模式的重复次数（1~100）
    raw_loop = spec.get("loop_count")
    try:
        loop_count = int(raw_loop) if raw_loop is not None else 1
    except (TypeError, ValueError):
        loop_count = 1
    loop_count = max(1, min(loop_count, 100))
    return {
        **spec,
        "schema_version": SCHEMA_VERSION,
        "variables": list(spec.get("variables") or []),
        "dataset_id": spec.get("dataset_id"),
        "dataset_mode": dataset_mode,
        "loop_count": loop_count,
        "fail_policy": fail_policy,
        "steps": steps,
    }

    # 031（US9）：loop 模式重复次数（1~100）
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
    # 031（US9）：loop 模式重复次数（1~100）
    raw_loop = spec.get("loop_count")
    if raw_loop not in (None, ""):
        try:
            loop_i = int(raw_loop)
        except (TypeError, ValueError):
            problems.append(f"loop_count 必须是整数，实际为 {raw_loop!r}")
        else:
            if not 1 <= loop_i <= 100:
                problems.append(f"loop_count 超出范围（1~100）: {loop_i}")
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
            elif btype in ("form_data", "multipart") and (
                body.get("files") or body.get("form_data")
            ):
                # 031：仅在提供结构化数组时按 multipart 校验（纯字符串 content 的旧用法不受影响）
                _validate_multipart(body, where, problems)
        retry_raw = raw.get("retry")
        if retry_raw is not None:
            _validate_retry(retry_raw, where, problems)
        st = raw.get("script_timeout_ms")
        if st not in (None, ""):
            try:
                st_i = int(st)
            except (TypeError, ValueError):
                problems.append(f"{where}.script_timeout_ms 必须是整数")
            else:
                if not 200 <= st_i <= 10000:
                    problems.append(f"{where}.script_timeout_ms 超出范围（200~10000）: {st_i}")
        for script_field in ("pre_script", "post_script"):
            script_text = str(raw.get(script_field) or "")
            if len(script_text) > MAX_SCRIPT_CHARS:
                problems.append(
                    f"{where}.{script_field} 超长（>{MAX_SCRIPT_CHARS} 字符，实际 {len(script_text)}）"
                )
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
            # 031（US6）：表达式断言必须给出非空表达式（沙箱求值，结果须为 bool）
            if atype == "expression" and not str(a.get("expression") or "").strip():
                problems.append(f"{where}.assertions[{j}] 表达式断言缺少 expression")
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
