# core/api_runner/request_builder.py
"""接口测试请求构造（029-api-testing T011/T012）。

契约：specs/029-api-testing/data-model.md §3 的 ``api_spec.steps[i].request`` 形状
（与 app/models/schemas.py 的 ApiRequestSpec 对齐）：

- 所有 url / header 值 / query 值 / body.content / auth 字段统一经
  ``core.api_runner.variables.render(value, scope)`` 渲染；未定义变量让
  ``UndefinedVariableError`` 冒泡（不吞异常）。
- URL：绝对地址（http/https 开头）直接用；相对路径拼 ``scope.lookup("baseUrl")``，
  缺失时抛 ``VariableError``；保留 url 已有 query，追加 query 列表项（同名后者覆盖）。
- headers：仅处理步骤头 + auth 注入；``enable=False`` 跳过；key 大小写不敏感去重
  （后者覆盖，保留最后写入的原始大小写）。
- body 五类型：none→None；json→渲染后字符串（非法 JSON 抛 ValueError，自动补
  Content-Type: application/json）；form→解析 ``a=1&b=2`` 为 dict；form_data/raw/
  binary→字符串原样（binary 不读文件）。
- auth：none 不注入；basic→``Authorization: Basic base64(u:p)``；bearer→
  ``Authorization: Bearer <token>``；api_key→按 ``in_``（header|query，默认 header）
  注入 ``key_name: key_value``。
- timeout_ms 钳制在 [1000, 300000]；follow_redirects / verify_ssl 默认 True。

纯函数：无网络、无 IO、无副作用；同步。
"""
from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import parse_qsl, urlencode

from core.api_runner.variables import VariableError, VariableScope, render

TIMEOUT_MIN_MS = 1000
TIMEOUT_MAX_MS = 300000
DEFAULT_TIMEOUT_MS = 30000
_JSON_CONTENT_TYPE = "application/json"


@dataclass
class RenderedRequest:
    """渲染完成的请求（发送器可直接消费）。

    ``content``：``bytes | str | dict | None``——dict 仅 form 类型（解析后的键值对）。
    ``auth``：``None`` 或 ``("basic", u, p)`` / ``("bearer", token)`` /
    ``("api_key", key_name, key_value, location)``。
    """

    method: str
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    params: dict[str, str] = field(default_factory=dict)
    content: Optional[Any] = None
    timeout_ms: int = DEFAULT_TIMEOUT_MS
    follow_redirects: bool = True
    verify_ssl: bool = True
    auth: Optional[tuple] = None


def _render_items(items: Any, scope: VariableScope) -> list[tuple[str, str]]:
    """渲染 ``[{key,value,enable}]`` → ``[(key, value)]``，跳过禁用项与空 key。"""
    out: list[tuple[str, str]] = []
    for it in items or []:
        if not isinstance(it, dict) or it.get("enable") is False:
            continue
        key = render(it.get("key"), scope)
        if not key:
            continue
        out.append((key, render(it.get("value"), scope)))
    return out


def _set_header(headers: dict[str, str], key: str, value: str) -> None:
    """大小写不敏感覆盖写入 header（保留新 key 的原始大小写）。"""
    for existing in list(headers):
        if existing.lower() == key.lower():
            del headers[existing]
    headers[key] = value


def _merge_headers(items: Any, scope: VariableScope) -> dict[str, str]:
    """渲染步骤头；key 大小写不敏感去重，后者覆盖，保留最后写入的原始大小写。"""
    headers: dict[str, str] = {}
    for key, value in _render_items(items, scope):
        _set_header(headers, key, value)
    return headers


def _resolve_url(url: str, scope: VariableScope) -> str:
    """绝对地址直接用；相对路径（/ 开头或裸路径）拼 baseUrl。"""
    if url.lower().startswith(("http://", "https://")):
        return url
    base = scope.lookup("baseUrl")
    if not base:
        raise VariableError(f"缺少 baseUrl，无法拼接相对 URL: {url}")
    return base.rstrip("/") + "/" + url.lstrip("/")


def _build_params(url: str, query_items: Any, scope: VariableScope) -> dict[str, str]:
    """合并 url 已有 query 与 query 列表项（同名后者覆盖）。"""
    _, _, existing_qs = url.partition("?")
    params: dict[str, str] = dict(parse_qsl(existing_qs, keep_blank_values=True))
    for key, value in _render_items(query_items, scope):
        params[key] = value
    return params


def _build_body(body: Any, headers: dict[str, str], scope: VariableScope) -> Any:
    """按 body.type 构造 content；json 校验 + 自动补 Content-Type。"""
    body_type = (body or {}).get("type") or "none"
    if body_type == "none":
        return None
    rendered = render((body or {}).get("content"), scope)
    if body_type == "json":
        try:
            json.loads(rendered)
        except (json.JSONDecodeError, TypeError) as exc:
            raise ValueError(f"JSON body 非法: {rendered[:80]!r}") from exc
        if not any(k.lower() == "content-type" for k in headers):
            headers["Content-Type"] = _JSON_CONTENT_TYPE
        return rendered
    if body_type == "form":
        # 渲染后再解析 a=1&b=2 → dict（保留空值）
        return dict(parse_qsl(rendered, keep_blank_values=True))
    # form_data / raw / binary：字符串原样（binary 不读文件）
    return rendered


def _build_auth(
    auth: Any,
    headers: dict[str, str],
    params: dict[str, str],
    scope: VariableScope,
) -> Optional[tuple]:
    """auth 注入（覆盖已有同名 header）；返回 auth 元组。"""
    auth_type = (auth or {}).get("type") or "none"
    if auth_type == "none":
        return None
    if auth_type == "basic":
        username = render(auth.get("username"), scope)
        password = render(auth.get("password"), scope)
        token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
        _set_header(headers, "Authorization", f"Basic {token}")
        return ("basic", username, password)
    if auth_type == "bearer":
        token = render(auth.get("token"), scope)
        _set_header(headers, "Authorization", f"Bearer {token}")
        return ("bearer", token)
    if auth_type == "api_key":
        key_name = render(auth.get("key_name"), scope)
        key_value = render(auth.get("key_value"), scope)
        location = (auth.get("in_") or "header").lower()
        if location == "query":
            params[key_name] = key_value
        else:
            _set_header(headers, key_name, key_value)
        return ("api_key", key_name, key_value, location)
    return None


def build_request(step: dict, scope: VariableScope) -> RenderedRequest:
    """把 ``api_spec.steps[i]`` 渲染为可发送的 ``RenderedRequest``。"""
    request = (step or {}).get("request") or {}
    method = str(request.get("method") or "GET")
    url = render(request.get("url"), scope)
    url = _resolve_url(url, scope)
    params = _build_params(url, request.get("query"), scope)
    headers = _merge_headers(request.get("headers"), scope)
    content = _build_body(request.get("body"), headers, scope)
    auth = _build_auth(request.get("auth"), headers, params, scope)
    if params:
        url = url.partition("?")[0] + "?" + urlencode(params)
    else:
        url = url.partition("?")[0]

    timeout_ms = request.get("timeout_ms")
    if timeout_ms is None:
        timeout_ms = DEFAULT_TIMEOUT_MS
    timeout_ms = max(TIMEOUT_MIN_MS, min(TIMEOUT_MAX_MS, int(timeout_ms)))

    return RenderedRequest(
        method=method,
        url=url,
        headers=headers,
        params=params,
        content=content,
        timeout_ms=timeout_ms,
        follow_redirects=bool(request.get("follow_redirects", True)),
        verify_ssl=bool(request.get("verify_ssl", True)),
        auth=auth,
    )


__all__ = [
    "TIMEOUT_MIN_MS",
    "TIMEOUT_MAX_MS",
    "DEFAULT_TIMEOUT_MS",
    "RenderedRequest",
    "build_request",
]