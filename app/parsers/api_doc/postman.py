# app/parsers/api_doc/postman.py
"""Postman Collection v2.1 解析（029-api-testing）。

- 递归展开 ``item`` 树（``item`` 为数组即分组，叶子含 ``request`` 即 operation）；
- ``request.url.path``（数组用 ``/`` 拼，``:id`` 保留）→ ``path``；
- ``request.url.query`` → ``request_schema.params``（in=query）；
- ``request.url.variable`` → ``request_schema.params``（in=path）；
- ``request.body``：``mode=raw`` → content_type 从 header 的 Content-Type 取或默认
  ``application/json``，``raw`` 文本尝试 ``json.loads`` 作为 example；
  ``mode=formdata`` → ``application/x-www-form-urlencoded``；
- Postman 变量（``{{baseUrl}}`` 等）**保留原样**写进 params/example，不做展开。

逐 operation 容错：单个 operation 解析失败只记 warnings（含 ``item[i]`` 定位），
不影响其余 operation。
"""
from __future__ import annotations

import json
from typing import Any

from .common import ParsedDocument, ParsedOperation

_DEFAULT_CONTENT_TYPE = "application/json"
_FORM_CONTENT_TYPE = "application/x-www-form-urlencoded"


def parse_postman(doc: dict) -> ParsedDocument:
    """解析 Postman Collection v2.1 文档。"""
    warnings: list[str] = []
    operations: list[ParsedOperation] = []
    items = doc.get("item")
    if not isinstance(items, list):
        warnings.append("item 缺失或不是数组")
        return ParsedDocument(source="postman", operations=operations, warnings=warnings)
    _walk_items(items, operations, warnings)
    return ParsedDocument(source="postman", operations=operations, warnings=warnings)


def _walk_items(items: list, operations: list[ParsedOperation], warnings: list[str]) -> None:
    """递归展开 item 树：含 ``request`` 的叶子 → operation；含 ``item`` 的分组 → 递归。"""
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        where = f"item[{i}]"
        if "request" in item:
            try:
                operations.append(_parse_operation(item, where))
            except Exception as exc:  # noqa: BLE001 —— 逐 operation 容错
                warnings.append(f"{where}: 解析失败: {exc}")
        elif isinstance(item.get("item"), list):
            _walk_items(item["item"], operations, warnings)


def _parse_operation(item: dict, where: str) -> ParsedOperation:
    request = item.get("request")
    if not isinstance(request, dict):
        raise ValueError(f"{where}.request 缺失或不是对象")
    method = str(request.get("method") or "GET").strip().upper()
    url = request.get("url")
    path, query, path_vars = _parse_url(url, where)
    headers = _parse_headers(request.get("header"))
    body = _parse_body(request.get("body"), headers)
    params = list(query) + list(path_vars)
    name = str(item.get("name") or "") or f"{method} {path}"
    return ParsedOperation(
        name=name,
        method=method,
        path=path,
        request_schema={"params": params, "body": body},
        response_schema={"statuses": {}},
    )


def _parse_url(url: Any, where: str) -> tuple[str, list[dict], list[dict]]:
    """解析 url → (path, query_params, path_params)。"""
    if isinstance(url, str):
        return url, [], []
    if not isinstance(url, dict):
        return "", [], []
    # path：数组用 / 拼（:id 保留）；字符串原样（补前导 /）
    path_parts = url.get("path")
    if isinstance(path_parts, list):
        path = "/" + "/".join(str(p) for p in path_parts if p is not None)
    elif isinstance(path_parts, str):
        path = path_parts if path_parts.startswith("/") else f"/{path_parts}"
    else:
        path = ""
    query: list[dict] = []
    for q in url.get("query") or []:
        if not isinstance(q, dict):
            continue
        query.append(
            {
                "name": str(q.get("key") or ""),
                "in": "query",
                "type": "string",
                "required": False,
                "example": q.get("value"),
                "enum": [],
                "default": q.get("value"),
            }
        )
    path_vars: list[dict] = []
    for v in url.get("variable") or []:
        if not isinstance(v, dict):
            continue
        path_vars.append(
            {
                "name": str(v.get("key") or ""),
                "in": "path",
                "type": "string",
                "required": True,
                "example": v.get("value"),
                "enum": [],
                "default": v.get("value"),
            }
        )
    return path, query, path_vars


def _parse_headers(raw: Any) -> list[dict]:
    headers: list[dict] = []
    if not isinstance(raw, list):
        return headers
    for h in raw:
        if isinstance(h, dict):
            headers.append({"key": str(h.get("key") or ""), "value": str(h.get("value") or "")})
    return headers


def _parse_body(raw: Any, headers: list[dict]) -> dict:
    """解析 request.body；无 body 时返回空结构。"""
    if not isinstance(raw, dict):
        return {"content_type": _DEFAULT_CONTENT_TYPE, "schema": {}, "example": {}}
    mode = str(raw.get("mode") or "")
    if mode == "raw":
        content_type = _DEFAULT_CONTENT_TYPE
        for h in headers:
            if str(h.get("key") or "").lower() == "content-type" and h.get("value"):
                content_type = str(h["value"])
                break
        raw_text = raw.get("raw")
        example: Any = {}
        if isinstance(raw_text, str) and raw_text.strip():
            try:
                example = json.loads(raw_text)
            except ValueError:
                example = raw_text
        return {"content_type": content_type, "schema": {}, "example": example}
    if mode == "formdata":
        return {"content_type": _FORM_CONTENT_TYPE, "schema": {}, "example": {}}
    return {"content_type": _DEFAULT_CONTENT_TYPE, "schema": {}, "example": {}}


__all__ = ["parse_postman"]