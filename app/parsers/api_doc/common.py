# app/parsers/api_doc/common.py
"""接口文档解析器：数据契约 + 统一入口 + schema→example 推导（029-api-testing）。

契约（specs/029-api-testing/data-model.md §1.1、contracts/api-test-contract.md §1.1）：
- ``ParsedOperation`` / ``ParsedDocument`` 是解析器与调用方（导入/生成）之间的冻结 DTO；
- ``detect_source`` 依据 ``openapi`` / ``swagger`` / ``info.schema`` 字段判定来源；
- ``parse_document`` 自适应 JSON/YAML，分发到 openapi / postman 解析器；
- ``schema_to_example`` 按 example > default > enum[0] > 类型造值 推导示例值；
- ``deref_schema`` 递归展开 ``$ref``（OpenAPI3 的 ``#/components/schemas/X`` 与
  Swagger2 的 ``#/definitions/X``），解析不了或循环引用时保留 ``{}`` 并记 warning，
  **绝不抛异常**。

本包为纯函数、无网络（swagger_url 的下载由调用方负责）。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import yaml

# 允许的 HTTP 方法（OpenAPI path item 中除这些之外的键视为扩展字段）
HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options"}


@dataclass
class ParsedOperation:
    """单个接口操作（解析后的统一 DTO）。"""

    name: str
    method: str
    path: str
    summary: str = ""
    operation_id: str = ""
    tags: str = ""
    request_schema: dict = field(default_factory=dict)
    response_schema: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


@dataclass
class ParsedDocument:
    """整份接口文档的解析结果。"""

    source: str  # "openapi3" | "swagger2" | "postman" | "har"
    operations: list[ParsedOperation]
    warnings: list[str] = field(default_factory=list)


# ── 加载与来源判定 ──────────────────────────────────────────────────────────


def _load_json_or_yaml(raw: bytes | str) -> Any:
    """JSON/YAML 自适应解析；两者都失败抛 ValueError。"""
    if isinstance(raw, bytes):
        text = raw.decode("utf-8")
    else:
        text = raw
    try:
        return json.loads(text)
    except ValueError:
        pass
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ValueError(f"JSON/YAML 解析失败: {exc}") from None


def _detect_from_doc(doc: Any) -> str:
    """依据已解析文档判定来源；无法识别抛 ValueError。"""
    if not isinstance(doc, dict):
        raise ValueError("接口文档必须是对象（dict）")
    if "openapi" in doc:
        return "openapi3"
    if "swagger" in doc:
        return "swagger2"
    log = doc.get("log")
    if isinstance(log, dict) and isinstance(log.get("entries"), list):
        return "har"
    info = doc.get("info")
    if isinstance(info, dict):
        schema = info.get("schema")
        if isinstance(schema, str) and "postman" in schema.lower():
            return "postman"
    raise ValueError("无法识别的接口文档格式（缺少 openapi/swagger/info.schema 字段）")


def detect_source(raw: bytes | str) -> str:
    """判定接口文档来源：``openapi3`` / ``swagger2`` / ``postman`` / ``har``。"""
    return _detect_from_doc(_load_json_or_yaml(raw))


def parse_document(raw: bytes | str, file_name: str = "") -> ParsedDocument:
    """解析接口文档（JSON/YAML 自适应），返回统一 DTO。

    ``file_name`` 仅用于错误信息定位，不影响解析结果。
    """
    doc = _load_json_or_yaml(raw)
    source = _detect_from_doc(doc)
    if source == "postman":
        from .postman import parse_postman

        return parse_postman(doc)
    if source == "har":
        from .har import parse_har

        return parse_har(doc)
    from .openapi import parse_openapi

    return parse_openapi(doc)


# ── $ref 展开 ───────────────────────────────────────────────────────────────


def deref_schema(
    node: Any,
    *,
    components: dict | None = None,
    definitions: dict | None = None,
    warnings: list[str] | None = None,
    where: str = "",
    _seen: set[str] | None = None,
) -> Any:
    """递归展开 ``$ref``；解析不了或循环引用 → ``{}`` 并记 warning（不抛异常）。

    - ``components``：OpenAPI3 的 ``components`` 对象（取 ``.schemas``）；
    - ``definitions``：Swagger2 的 ``definitions`` 对象；
    - ``where``：定位串（如 ``paths./api/login.post.requestBody``），用于 warnings。
    """
    if warnings is None:
        warnings = []
    if _seen is None:
        _seen = set()
    if isinstance(node, list):
        return [deref_schema(i, components=components, definitions=definitions, warnings=warnings, where=where, _seen=_seen) for i in node]
    if not isinstance(node, dict):
        return node
    ref = node.get("$ref")
    if not isinstance(ref, str):
        return {k: deref_schema(v, components=components, definitions=definitions, warnings=warnings, where=where, _seen=_seen) for k, v in node.items()}
    return _resolve_ref(ref, components=components, definitions=definitions, warnings=warnings, where=where, _seen=_seen)


def _resolve_ref(
    ref: str,
    *,
    components: dict | None,
    definitions: dict | None,
    warnings: list[str],
    where: str,
    _seen: set[str],
) -> dict:
    """解析单个 ``$ref`` 引用并递归展开目标。"""
    name: str | None = None
    target: Any = None
    if ref.startswith("#/components/schemas/"):
        name = ref[len("#/components/schemas/") :]
        schemas = (components or {}).get("schemas") if isinstance(components, dict) else None
        target = schemas.get(name) if isinstance(schemas, dict) else None
    elif ref.startswith("#/definitions/"):
        name = ref[len("#/definitions/") :]
        target = definitions.get(name) if isinstance(definitions, dict) else None
    else:
        warnings.append(f"{where}: 不支持的 $ref {ref!r}")
        return {}
    if not isinstance(target, dict):
        warnings.append(f"{where}: $ref {ref!r} 无法解析")
        return {}
    if name in _seen:
        warnings.append(f"{where}: $ref 循环引用 {ref!r}")
        return {}
    return deref_schema(target, components=components, definitions=definitions, warnings=warnings, where=where, _seen=_seen | {name})


# ── schema → example ────────────────────────────────────────────────────────


def schema_to_example(schema: dict) -> Any:
    """按 example > default > enum[0] > 类型造值 推导示例值。

    - object：递归 ``properties`` 生成 ``{key: example}``；
    - array：递归 ``items`` 生成单元素列表；
    - 无类型信息（含未展开的 ``$ref``）→ ``None``。
    """
    if not isinstance(schema, dict):
        return None
    if "example" in schema:
        return schema["example"]
    if "default" in schema:
        return schema["default"]
    if isinstance(schema.get("enum"), list) and schema["enum"]:
        return schema["enum"][0]
    stype = schema.get("type")
    if stype == "object" or (stype is None and isinstance(schema.get("properties"), dict)):
        props = schema.get("properties") or {}
        return {k: schema_to_example(v) for k, v in props.items()}
    if stype == "array":
        items = schema.get("items")
        return [schema_to_example(items)] if isinstance(items, dict) else []
    if stype == "string":
        return ""
    if stype == "integer":
        return 0
    if stype == "number":
        return 0.0
    if stype == "boolean":
        return False
    if stype == "null":
        return None
    return None


__all__ = [
    "ParsedOperation",
    "ParsedDocument",
    "detect_source",
    "parse_document",
    "schema_to_example",
    "deref_schema",
    "HTTP_METHODS",
]