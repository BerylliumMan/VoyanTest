# app/gen/api/generator.py
"""接口用例规则式生成器（029-api-testing T024）。

纯函数、同步、无网络、无 IO、无 LLM：完全基于 api_definitions 的
request_schema / response_schema 按规则生成参数化接口用例草案。

契约（specs/029-api-testing/spec.md FR-3、data-model.md §3、
contracts/api-test-contract.md §1.3）：
- ``generate_cases_for_definition`` 返回
  ``[{"name", "priority", "api_spec", "notes"}]``；
- 每条 ``api_spec`` 必须能被 ``core.api_spec.normalize_api_spec`` 解析、
  且 ``validate_api_spec`` 返回空列表；
- 参数值来源优先级：example > default > enum[0] > 类型造值；
  全无则用 ``{{参数名}}`` 占位并在 notes 记录（不编造）。
"""
from __future__ import annotations

import json
from typing import Any

DEFAULT_OPTIONS: dict[str, Any] = {
    "normal": True,
    "boundary": True,
    "missing_required": True,
    "type_error": False,
    "auth_fail": False,
    "max_cases_per_operation": 5,
    # multipart/form-data（文件上传）暂不支持：编码与文件源都不到位，生成出来必然执行失败
    "skip_multipart": True,
}

# 类型造值（正常用例兜底的具体值）
_TYPE_VALUES: dict[str, Any] = {
    "string": "test",
    "integer": 1,
    "number": 1.5,
    "boolean": True,
    "array": [],
    "object": {},
}

# 类型错误用例的错误值（integer 传 "abc" 等）
_TYPE_ERROR_VALUES: dict[str, Any] = {
    "integer": "abc",
    "number": "abc",
    "string": 123,
    "boolean": "not-a-boolean",
    "array": "not-an-array",
    "object": "not-an-object",
}

_KNOWN_TYPES = set(_TYPE_VALUES) | set(_TYPE_ERROR_VALUES)


def _placeholder(name: str) -> str:
    return "{{" + name + "}}"


def _pick_value(name: str, field: dict, notes: list[str]) -> Any:
    """按 example > default > enum[0] > 类型造值 取具体值；全无则占位。"""
    if field.get("example") is not None:
        return field["example"]
    if field.get("default") is not None:
        return field["default"]
    enum = field.get("enum") or []
    if enum:
        return enum[0]
    ptype = field.get("type")
    if ptype in _TYPE_VALUES:
        return _TYPE_VALUES[ptype]
    notes.append(f"缺少示例值，已用 {_placeholder(name)} 占位")
    return _placeholder(name)


def _expected_status(response_schema: Any) -> str:
    """期望状态码：statuses 里第一个 2xx，无则 200。"""
    statuses = (response_schema or {}).get("statuses") or {}
    for code in statuses:
        if str(code).startswith("2"):
            return str(code)
    return "200"


def _first_2xx_example(response_schema: Any) -> Any:
    statuses = (response_schema or {}).get("statuses") or {}
    for code, info in statuses.items():
        if str(code).startswith("2") and isinstance(info, dict):
            return info.get("example")
    return None


def _jsonpath_expression(example: Any) -> str | None:
    """example 是对象 → 断言第一个字段存在（$.<key>）。"""
    if isinstance(example, dict) and example:
        return "$." + next(iter(example))
    return None


def _build_assertions(
    definition: dict, *, with_jsonpath: bool, negative: bool = False
) -> list[dict]:
    """构造断言；``negative=True`` 用于缺参数/类型错误/错误凭据等**负向场景**。

    负向场景期望 4xx：用 ``status_code >= 400`` 表达（既覆盖 400/401/403/422，
    也不把 5xx 服务端错误误判为「符合预期」时再加一条 ``< 500``）。
    """
    if negative:
        return [
            {
                "enable": True,
                "type": "status_code",
                "condition": "gte",
                "expected": "400",
                "name": "状态码 >= 400",
            },
            {
                "enable": True,
                "type": "status_code",
                "condition": "lt",
                "expected": "500",
                "name": "状态码 < 500",
            },
        ]
    status = _expected_status(definition.get("response_schema"))
    asserts: list[dict] = [
        {
            "enable": True,
            "type": "status_code",
            "condition": "equals",
            "expected": status,
            "name": f"状态码 {status}",
        }
    ]
    if with_jsonpath:
        expr = _jsonpath_expression(_first_2xx_example(definition.get("response_schema")))
        if expr:
            asserts.append(
                {
                    "enable": True,
                    "type": "jsonpath",
                    "expression": expr,
                    "condition": "exists",
                    "expected": "",
                    "name": f"响应包含 {expr}",
                }
            )
    return asserts


def _fill_path(path: str, values: dict[str, Any]) -> str:
    for name, value in values.items():
        path = path.replace("{" + name + "}", str(value))
    return path


def _build_body(body_schema: Any, notes: list[str]) -> dict | None:
    """body.example 优先；无则按 schema.properties 逐字段造值；都无 → None。"""
    if not isinstance(body_schema, dict) or not body_schema:
        return None
    example = body_schema.get("example")
    if example is not None:
        return {"type": "json", "content": json.dumps(example, ensure_ascii=False)}
    schema = body_schema.get("schema")
    if isinstance(schema, dict) and isinstance(schema.get("properties"), dict):
        obj = {}
        for fname, fschema in schema["properties"].items():
            obj[fname] = _pick_value(fname, fschema, notes)
        return {"type": "json", "content": json.dumps(obj, ensure_ascii=False)}
    return None


def _normal_request(definition: dict, notes: list[str]) -> dict:
    """构造正常用例的 request（URL/headers/query/body/auth）。"""
    method = str(definition.get("method") or "GET").strip().upper()
    path = str(definition.get("path") or "")
    req_schema = definition.get("request_schema") or {}
    params = req_schema.get("params") or []
    body_schema = req_schema.get("body") or {}

    path_values: dict[str, Any] = {}
    query: list[dict] = []
    headers: list[dict] = []
    for p in params:
        if not isinstance(p, dict) or not p.get("name"):
            continue
        pin = p.get("in")
        if pin == "path":
            path_values[p["name"]] = _pick_value(p["name"], p, notes)
        elif pin == "query":
            query.append({"key": p["name"], "value": _pick_value(p["name"], p, notes), "enable": True})
        elif pin == "header":
            headers.append({"key": p["name"], "value": _pick_value(p["name"], p, notes), "enable": True})

    body = _build_body(body_schema, notes)
    if body is not None:
        content_type = body_schema.get("content_type") or "application/json"
        headers.insert(0, {"key": "Content-Type", "value": content_type, "enable": True})

    auth = definition.get("auth") or {}
    auth = {**auth, "type": str(auth.get("type") or "none")}

    return {
        "method": method,
        "url": "{{baseUrl}}" + _fill_path(path, path_values),
        "headers": headers,
        "query": query,
        "body": body or {"type": "none", "content": ""},
        "auth": auth,
        "timeout_ms": 30000,
        "follow_redirects": True,
        "verify_ssl": True,
    }


def _make_case(
    definition: dict,
    name: str,
    priority: str,
    request: dict,
    assertions: list[dict],
    notes: list[str],
    login_step: dict | None = None,
) -> dict:
    steps = [
        {
            "order": 1,
            "name": name,
            "enable": True,
            "definition_id": definition.get("id"),
            "request": request,
            "assertions": assertions,
            "extractors": [],
            "pre": [],
            "post": [],
        }
    ]
    if login_step is not None:  # 需要鉴权：登录步骤前置，目标调用顺延为第 2 步
        steps[0]["order"] = 2
        login_step["order"] = 1
        steps.insert(0, login_step)
    return {
        "name": name,
        "priority": priority,
        "api_spec": {
            "schema_version": 1,
            "variables": [],
            "dataset_id": None,
            "fail_policy": "fail_fast",
            "steps": steps,
        },
        "notes": notes,
    }


def _normal_case(definition: dict) -> dict:
    notes: list[str] = []
    request = _normal_request(definition, notes)
    name = f"{definition.get('name') or '接口'} - 正常流"
    return _make_case(
        definition, name, "high", request,
        _build_assertions(definition, with_jsonpath=True), notes,
    )


def _body_fields(definition: dict) -> list[tuple[str, dict]]:
    """body.schema.properties 的 (字段名, 字段 schema) 列表。"""
    req_schema = definition.get("request_schema") or {}
    body_schema = req_schema.get("body") or {}
    schema = body_schema.get("schema") if isinstance(body_schema, dict) else None
    if isinstance(schema, dict) and isinstance(schema.get("properties"), dict):
        return [(fname, fschema) for fname, fschema in schema["properties"].items()]
    return []


def _apply_field_value(request: dict, fname: str, value: Any) -> None:
    """把值应用到 request 的对应位置（path URL / query / header / body）。"""
    if "{" + fname + "}" in request["url"]:
        request["url"] = request["url"].replace("{" + fname + "}", str(value))
        return
    for q in request["query"]:
        if q["key"] == fname:
            q["value"] = value
            return
    for h in request["headers"]:
        if h["key"] == fname:
            h["value"] = value
            return
    body = request["body"]
    if body.get("type") == "json" and body.get("content"):
        try:
            obj = json.loads(body["content"])
        except ValueError:
            obj = None
        if isinstance(obj, dict) and fname in obj:
            obj[fname] = value
            body["content"] = json.dumps(obj, ensure_ascii=False)


def _missing_required_cases(definition: dict) -> list[dict]:
    """每个 required 的 query/header 参数或 body 字段生成 1 条（移除该项）。

    path 参数为必填时不生成——path 缺了 URL 不成立。
    """
    req_schema = definition.get("request_schema") or {}
    params = req_schema.get("params") or []
    required_fields: list[tuple[str, str]] = []  # (kind, name)
    for p in params:
        if not isinstance(p, dict) or not p.get("name"):
            continue
        if p.get("in") in ("query", "header") and p.get("required"):
            required_fields.append(("param", p["name"]))
    for fname, _fschema in _body_fields(definition):
        body_schema = (req_schema.get("body") or {})
        schema = body_schema.get("schema") if isinstance(body_schema, dict) else None
        if isinstance(schema, dict) and fname in (schema.get("required") or []):
            required_fields.append(("body", fname))

    cases: list[dict] = []
    for kind, fname in required_fields:
        notes = [f"缺少必填参数 {fname}"]
        request = _normal_request(definition, [])
        if kind == "param":
            request["query"] = [q for q in request["query"] if q["key"] != fname]
            request["headers"] = [h for h in request["headers"] if h["key"] != fname]
        else:
            body = request["body"]
            if body.get("type") == "json" and body.get("content"):
                try:
                    obj = json.loads(body["content"])
                except ValueError:
                    obj = None
                if isinstance(obj, dict):
                    obj.pop(fname, None)
                    body["content"] = json.dumps(obj, ensure_ascii=False)
        name = f"{definition.get('name') or '接口'} - 缺少必填参数 {fname}"
        cases.append(
            _make_case(
                definition, name, "medium", request,
                _build_assertions(definition, with_jsonpath=False, negative=True), notes,
            )
        )
    return cases


def _boundary_value(field: dict) -> Any | None:
    """enum 最后一项优先；数值带 maximum/minimum 用边界值；都无 → None。"""
    enum = field.get("enum") or []
    if enum:
        return enum[-1]
    ftype = field.get("type")
    if ftype in ("integer", "number"):
        if field.get("maximum") is not None:
            return field["maximum"]
        if field.get("minimum") is not None:
            return field["minimum"]
    return None


def _boundary_cases(definition: dict) -> list[dict]:
    """每个有边界条件的参数/body 字段生成 1 条边界用例。"""
    req_schema = definition.get("request_schema") or {}
    fields: list[tuple[str, Any]] = []
    for p in req_schema.get("params") or []:
        if not isinstance(p, dict) or not p.get("name"):
            continue
        bv = _boundary_value(p)
        if bv is not None:
            fields.append((p["name"], bv))
    for fname, fschema in _body_fields(definition):
        bv = _boundary_value(fschema)
        if bv is not None:
            fields.append((fname, bv))

    cases: list[dict] = []
    for fname, bv in fields:
        notes = [f"边界值：{fname}={bv}"]
        request = _normal_request(definition, [])
        _apply_field_value(request, fname, bv)
        name = f"{definition.get('name') or '接口'} - 边界值 {fname}"
        cases.append(
            _make_case(
                definition, name, "low", request,
                _build_assertions(definition, with_jsonpath=False), notes,
            )
        )
    return cases


def _type_error_cases(definition: dict) -> list[dict]:
    """每个类型明确的参数/body 字段生成 1 条类型错误用例。"""
    req_schema = definition.get("request_schema") or {}
    fields: list[tuple[str, Any]] = []
    for p in req_schema.get("params") or []:
        if not isinstance(p, dict) or not p.get("name"):
            continue
        if p.get("type") in _TYPE_ERROR_VALUES:
            fields.append((p["name"], _TYPE_ERROR_VALUES[p["type"]]))
    for fname, fschema in _body_fields(definition):
        if fschema.get("type") in _TYPE_ERROR_VALUES:
            fields.append((fname, _TYPE_ERROR_VALUES[fschema["type"]]))

    cases: list[dict] = []
    for fname, bad in fields:
        notes = [f"类型错误：{fname} 传 {bad!r}"]
        request = _normal_request(definition, [])
        _apply_field_value(request, fname, bad)
        name = f"{definition.get('name') or '接口'} - 类型错误 {fname}"
        cases.append(
            _make_case(
                definition, name, "low", request,
                _build_assertions(definition, with_jsonpath=False, negative=True), notes,
            )
        )
    return cases


def _is_multipart(definition: dict) -> bool:
    body = (definition.get("request_schema") or {}).get("body") or {}
    return str(body.get("content_type") or "").lower().startswith("multipart/form-data")


def _auth_requirements(definition: dict) -> list[dict]:
    """definition 声明的鉴权要求（来自解析器的 request_schema.security）。"""
    req_schema = definition.get("request_schema") or {}
    security = req_schema.get("security")
    if isinstance(security, list):
        return [s for s in security if isinstance(s, dict)]
    auth = definition.get("auth") or {}
    atype = str(auth.get("type") or "none")
    if atype in ("bearer", "basic"):
        return [{"name": atype, "type": "http", "scheme": atype, "in": "", "key_name": ""}]
    return []


def _apply_auth_header(
    request: dict, security: list[dict], notes: list[str], *, invalid: bool = False
) -> None:
    """按鉴权方案注入凭据：注入的是 ``{{token}}`` 变量，由登录步骤的提取器填充。"""
    for scheme in security:
        stype = str(scheme.get("type") or "")
        sname = str(scheme.get("scheme") or "")
        if stype == "http" and sname == "bearer":
            value = "Bearer invalid-token" if invalid else "Bearer {{token}}"
        elif (stype == "http" and sname == "basic") or stype == "basic":
            value = "Basic invalid" if invalid else "Basic {{token}}"
        elif stype in ("apiKey", "api_key"):
            key_name = str(scheme.get("key_name") or "X-API-Key")
            value = "invalid-api-key" if invalid else "{{apiKey}}"
            if str(scheme.get("in")) == "query":
                request.setdefault("query", []).append({"key": key_name, "value": value, "enable": True})
            else:
                request["headers"] = [
                    h for h in request.get("headers", []) if str(h.get("key", "")).lower() != key_name.lower()
                ]
                request["headers"].append({"key": key_name, "value": value, "enable": True})
            return
        else:
            continue
        request["headers"] = [
            h for h in request.get("headers", []) if str(h.get("key", "")).lower() != "authorization"
        ]
        request["headers"].append({"key": "Authorization", "value": value, "enable": True})
        return
    notes.append("声明了鉴权但方案无法识别，未注入凭据")


def _login_step(auth_definition: dict) -> dict:
    """前置登录步骤：请求模板取自登录接口定义，提取器把响应里的 token 写进 case 作用域。"""
    login_notes: list[str] = []
    request = _normal_request(auth_definition, login_notes)
    return {
        "order": 1,
        "name": f"登录获取凭据（{auth_definition.get('name') or auth_definition.get('path') or '登录接口'}）",
        "enable": True,
        "definition_id": auth_definition.get("id"),
        "request": request,
        # 登录步骤自带 2xx 断言：登录失败时在第一步就给出明确失败原因，而不是让后续步骤 401
        "assertions": _build_assertions(auth_definition, with_jsonpath=False),
        "extractors": [
            {
                "enable": True,
                "type": "jsonpath",
                "expression": "$.token",
                "variable": "token",
                "scope": "case",
                "required": False,
            },
            {
                "enable": True,
                "type": "jsonpath",
                "expression": "$.data.token",
                "variable": "token",
                "scope": "case",
                "required": False,
            },
        ],
        "pre": [],
        "post": [],
    }


def _auth_fail_cases(definition: dict) -> list[dict]:
    """需要鉴权时生成 1 条「错误凭据」负向用例（期望 4xx）。"""
    security = _auth_requirements(definition)
    if not security:
        return []
    notes = ["使用错误凭据"]
    request = _normal_request(definition, [])
    _apply_auth_header(request, security, notes, invalid=True)
    name = f"{definition.get('name') or '接口'} - 错误凭据"
    return [
        _make_case(
            definition, name, "low", request,
            _build_assertions(definition, with_jsonpath=False, negative=True), notes,
        )
    ]


def generate_cases_for_definition(
    definition: dict,
    options: dict | None = None,
    *,
    auth_definition: dict | None = None,
) -> list[dict]:
    """按规则生成接口用例草案（纯函数，无 LLM/网络/IO）。

    options 键（缺省全 True 除 type_error/auth_fail）：
    normal / boundary / missing_required / type_error / auth_fail /
    max_cases_per_operation（默认 5）。
    截断按 正常 > 必填缺失 > 边界 > 类型错误 > 鉴权失败 的优先级保留。

    ``auth_definition``：需要鉴权时的登录接口定义。给了它就生成「登录 + 目标调用」
    两步用例（token 由提取器写入 case 作用域）；没给则只注入 ``{{token}}`` 并在 notes 提示。
    """
    opts = {**DEFAULT_OPTIONS, **(options or {})}
    if opts.get("skip_multipart") and _is_multipart(definition):
        return []
    try:
        max_cases = int(opts.get("max_cases_per_operation", 5))
    except (TypeError, ValueError):
        max_cases = 5

    tagged: list[tuple[str, dict]] = []
    if opts.get("normal"):
        tagged.append(("normal", _normal_case(definition)))
    if opts.get("missing_required"):
        tagged.extend(("missing", c) for c in _missing_required_cases(definition))
    if opts.get("boundary"):
        tagged.extend(("boundary", c) for c in _boundary_cases(definition))
    if opts.get("type_error"):
        tagged.extend(("type_error", c) for c in _type_error_cases(definition))
    if opts.get("auth_fail"):
        tagged.extend(("auth_fail", c) for c in _auth_fail_cases(definition))
    tagged = tagged[:max_cases]

    security = _auth_requirements(definition)
    if security:
        for scenario, case in tagged:
            if scenario == "auth_fail":
                continue  # 错误凭据用例**不能**带登录步骤，否则测不到 401
            target = case["api_spec"]["steps"][-1]
            _apply_auth_header(target["request"], security, case["notes"])
            if auth_definition:
                login = _login_step(auth_definition)
                target["order"] = 2
                case["api_spec"]["steps"] = [login, target]
            else:
                case["notes"] = list(
                    dict.fromkeys(
                        [*(case.get("notes") or []), "需在环境变量中提供 token（未找到登录接口定义）"]
                    )
                )
    return [case for _, case in tagged]


__all__ = ["generate_cases_for_definition", "DEFAULT_OPTIONS"]