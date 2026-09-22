# app/parsers/api_doc/openapi.py
"""OpenAPI 3.x 与 Swagger 2.0 解析（029-api-testing）。

- OpenAPI3：``paths`` → operations；``parameters``（query/path/header）→ ``request_schema.params``；
  ``requestBody.content.<ct>.schema`` → ``request_schema.body``；``responses.<code>.content`` → ``response_schema``；
  ``$ref`` 走 ``#/components/schemas/X``。
- Swagger2 归一：``host``/``basePath``/``schemes`` 不写进 path（path 只留路径）；
  ``in: body`` 参数 → ``body``；``in: formData`` → ``content_type: application/x-www-form-urlencoded``；
  query 参数类型在 parameter 上（``type``/``default``/``enum``）；``$ref`` 走 ``#/definitions/X``。

逐 operation 容错：单个 operation 解析失败只记 warnings（含 ``paths.<path>.<method>`` 定位），
不影响其余 operation。
"""
from __future__ import annotations

from typing import Any

from .common import (
    HTTP_METHODS,
    ParsedDocument,
    ParsedOperation,
    deref_schema,
    schema_to_example,
)

# 参数位置白名单（其余 in 值忽略）
_PARAM_LOCATIONS = {"query", "path", "header"}


def parse_openapi(doc: dict) -> ParsedDocument:
    """解析 OpenAPI3 / Swagger2 文档（依据 ``openapi`` / ``swagger`` 字段区分）。"""
    source = "openapi3" if "openapi" in doc else "swagger2"
    components = doc.get("components") if isinstance(doc.get("components"), dict) else None
    definitions = doc.get("definitions") if isinstance(doc.get("definitions"), dict) else None
    # 鉴权方案：OpenAPI3 在 components.securitySchemes，Swagger2 在 securityDefinitions
    schemes: dict = {}
    if isinstance(components, dict) and isinstance(components.get("securitySchemes"), dict):
        schemes = components["securitySchemes"]
    elif isinstance(doc.get("securityDefinitions"), dict):
        schemes = doc["securityDefinitions"]
    doc_security = doc.get("security") if isinstance(doc.get("security"), list) else []
    warnings: list[str] = []
    operations: list[ParsedOperation] = []
    paths = doc.get("paths")
    if not isinstance(paths, dict):
        warnings.append("paths 缺失或不是对象")
        return ParsedDocument(source=source, operations=operations, warnings=warnings)
    for path, path_item in paths.items():
        if not isinstance(path_item, dict):
            continue
        for method, op in path_item.items():
            if method not in HTTP_METHODS:
                continue
            where = f"paths.{path}.{method}"
            try:
                operations.append(
                    _parse_operation(
                        path, method, op, components, definitions, warnings, where,
                        schemes=schemes, doc_security=doc_security,
                    )
                )
            except Exception as exc:  # noqa: BLE001 —— 逐 operation 容错，不中断整份文档
                warnings.append(f"{where}: 解析失败: {exc}")
    return ParsedDocument(source=source, operations=operations, warnings=warnings)


def _resolve_security(
    op: dict, schemes: dict | None, doc_security: list
) -> list[dict]:
    """把 security / securitySchemes 归一为 ``[{"name","type","scheme","in","key_name"}]``。

    operation 级 ``security`` 优先于文档级；``security: []`` 显式表示「无需鉴权」。
    """
    entries = op.get("security")
    if not isinstance(entries, list):
        entries = doc_security if isinstance(doc_security, list) else []
    resolved: list[dict] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        for scheme_name in entry:
            spec = (schemes or {}).get(scheme_name)
            spec = spec if isinstance(spec, dict) else {}
            stype = str(spec.get("type") or "")
            resolved.append(
                {
                    "name": str(scheme_name),
                    "type": stype,
                    "scheme": str(spec.get("scheme") or "") if stype == "http" else "",
                    "in": str(spec.get("in") or ""),
                    "key_name": str(spec.get("name") or ""),
                }
            )
    return resolved


def _parse_operation(
    path: str,
    method: str,
    op: Any,
    components: dict | None,
    definitions: dict | None,
    warnings: list[str],
    where: str,
    *,
    schemes: dict | None = None,
    doc_security: list | None = None,
) -> ParsedOperation:
    if not isinstance(op, dict):
        raise ValueError(f"{where} 必须是对象（dict），实际为 {type(op).__name__}")
    summary = str(op.get("summary") or "")
    operation_id = str(op.get("operationId") or op.get("operation_id") or "")
    tags = ",".join(str(t) for t in (op.get("tags") or []) if t)
    name = summary or operation_id or f"{method.upper()} {path}"
    return ParsedOperation(
        name=name,
        method=method.upper(),
        path=path,
        summary=summary,
        operation_id=operation_id,
        tags=tags,
        request_schema=_parse_request(
            op,
            components,
            definitions,
            warnings,
            where,
            security=_resolve_security(op, schemes, doc_security or []),
        ),
        response_schema=_parse_responses(op, components, definitions, warnings, where),
    )


def _param_schema(p: dict, components: dict | None, definitions: dict | None, warnings: list[str], where: str) -> dict:
    """取参数 schema：OpenAPI3 在 ``p.schema``，Swagger2 直接在 parameter 上。"""
    schema = p.get("schema")
    if not isinstance(schema, dict):
        schema = p
    return deref_schema(schema, components=components, definitions=definitions, warnings=warnings, where=where)


def _parse_request(
    op: dict,
    components: dict | None,
    definitions: dict | None,
    warnings: list[str],
    where: str,
    *,
    security: list[dict] | None = None,
) -> dict:
    params: list[dict] = []
    body: dict = {"content_type": "application/json", "schema": {}, "example": {}}
    form_props: dict[str, dict] = {}
    form_required: list[str] = []
    for i, p in enumerate(op.get("parameters") or []):
        if not isinstance(p, dict):
            continue
        pin = str(p.get("in") or "")
        pwhere = f"{where}.parameters[{i}]"
        if pin in _PARAM_LOCATIONS:
            schema = _param_schema(p, components, definitions, warnings, pwhere)
            params.append(
                {
                    "name": str(p.get("name") or ""),
                    "in": pin,
                    "type": schema.get("type") or "",
                    "required": bool(p.get("required", False)),
                    "example": schema.get("example"),
                    "enum": schema.get("enum") or [],
                    "default": schema.get("default"),
                }
            )
        elif pin == "body":  # Swagger2：in: body → body
            schema = p.get("schema") or {}
            schema = deref_schema(schema, components=components, definitions=definitions, warnings=warnings, where=pwhere)
            body = {"content_type": "application/json", "schema": schema, "example": schema_to_example(schema)}
        elif pin == "formData":  # Swagger2：in: formData → urlencoded body（合并为 object schema）
            schema = _param_schema(p, components, definitions, warnings, pwhere)
            name = str(p.get("name") or "")
            form_props[name] = {k: v for k, v in schema.items() if k in {"type", "format", "default", "enum", "example", "items"}}
            if p.get("required"):
                form_required.append(name)
    if form_props:
        form_schema: dict = {"type": "object", "properties": form_props}
        if form_required:
            form_schema["required"] = form_required
        body = {
            "content_type": "application/x-www-form-urlencoded",
            "schema": form_schema,
            "example": schema_to_example(form_schema),
        }
    # OpenAPI3：requestBody.content.<ct>.schema
    request_body = op.get("requestBody")
    if isinstance(request_body, dict):
        content = request_body.get("content")
        if isinstance(content, dict):
            for ct, ct_obj in content.items():
                if isinstance(ct_obj, dict):
                    schema = ct_obj.get("schema") or {}
                    schema = deref_schema(schema, components=components, definitions=definitions, warnings=warnings, where=f"{where}.requestBody")
                    body = {"content_type": ct, "schema": schema, "example": schema_to_example(schema)}
                break
    return {"params": params, "body": body, "security": security or []}


def _parse_responses(
    op: dict,
    components: dict | None,
    definitions: dict | None,
    warnings: list[str],
    where: str,
) -> dict:
    statuses: dict[str, dict] = {}
    responses = op.get("responses")
    if not isinstance(responses, dict):
        return {"statuses": statuses}
    for status, resp in responses.items():
        if not isinstance(resp, dict):
            continue
        rwhere = f"{where}.responses.{status}"
        entry: dict = {"schema": {}, "example": {}}
        content = resp.get("content")
        if isinstance(content, dict):  # OpenAPI3
            for ct, ct_obj in content.items():
                if isinstance(ct_obj, dict) and "schema" in ct_obj:
                    schema = ct_obj.get("schema") or {}
                    schema = deref_schema(schema, components=components, definitions=definitions, warnings=warnings, where=rwhere)
                    entry = {"schema": schema, "example": schema_to_example(schema)}
                break
        elif "schema" in resp:  # Swagger2
            schema = resp.get("schema") or {}
            schema = deref_schema(schema, components=components, definitions=definitions, warnings=warnings, where=rwhere)
            entry = {"schema": schema, "example": schema_to_example(schema)}
        statuses[str(status)] = entry
    return {"statuses": statuses}


__all__ = ["parse_openapi"]