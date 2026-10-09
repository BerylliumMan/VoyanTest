"""033（US11）：代码片段生成器（cURL / Python requests / JavaScript fetch）。

服务端生成（CLI/CI 与前端共用口径）；**输入应先经 ``mask_snippet_inputs`` 打码**，
本模块只负责格式化，不做安全处理。
"""

from __future__ import annotations

from typing import Any, Optional

__all__ = ["build_snippets", "mask_snippet_inputs"]


def _is_multipart(headers: dict[str, str]) -> bool:
    return any(
        str(k).lower() == "content-type" and "multipart" in str(v).lower()
        for k, v in (headers or {}).items()
    )


def _curl_escape(value: Any) -> str:
    return str(value).replace("'", "'\\''")


def build_curl(method: str, url: str, headers: dict[str, str], body_text: Optional[str]) -> str:
    headers = headers or {}
    parts = [f"curl -X {str(method).upper()} '{_curl_escape(url)}'"]
    for key, value in headers.items():
        if str(key).lower() == "content-length":
            continue
        if _is_multipart(headers) and str(key).lower() == "content-type":
            continue
        parts.append(f"  -H '{_curl_escape(key)}: {_curl_escape(value)}'")
    if _is_multipart(headers):
        parts.append("  -F 'file=@/path/to/file'   # multipart：按需补充各字段与文件")
    elif body_text:
        parts.append(f"  --data-raw '{_curl_escape(body_text)}'")
    return " \\\n".join(parts)


def build_python(method: str, url: str, headers: dict[str, str], body_text: Optional[str]) -> str:
    import json as _json

    headers = headers or {}
    lines = [
        "import requests",
        "",
        f"url = {url!r}",
        f"headers = {_json.dumps(headers, ensure_ascii=False)}",
    ]
    call = f"resp = requests.request({str(method).upper()!r}, url, headers=headers"
    if body_text:
        content_type = next(
            (str(v) for k, v in headers.items() if str(k).lower() == "content-type"), ""
        )
        if "json" in content_type.lower():
            lines.append(f"payload = {body_text}")
            call += ", json=payload"
        elif _is_multipart(headers):
            call += ", files={'file': open('/path/to/file', 'rb')}"
        else:
            call += f", data={body_text!r}"
    call += ")"
    lines.append(call)
    lines.append("print(resp.status_code, resp.text[:500])")
    return "\n".join(lines)


def build_js(method: str, url: str, headers: dict[str, str], body_text: Optional[str]) -> str:
    import json as _json

    headers = headers or {}
    lines = [
        f"const resp = await fetch({_json.dumps(url)}, {{",
        f"  method: {_json.dumps(str(method).upper())},",
        f"  headers: {_json.dumps(headers, ensure_ascii=False)},",
    ]
    if body_text:
        lines.append(f"  body: {_json.dumps(body_text, ensure_ascii=False)},")
    lines.append("});")
    lines.append("console.log(resp.status, await resp.text());")
    return "\n".join(lines)


def build_snippets(
    method: str,
    url: str,
    headers: Optional[dict[str, str]] = None,
    body_text: Optional[str] = None,
) -> dict:
    headers = headers or {}
    return {
        "curl": build_curl(method, url, headers, body_text),
        "python": build_python(method, url, headers, body_text),
        "js": build_js(method, url, headers, body_text),
    }


def mask_snippet_inputs(
    *,
    url: str,
    headers: dict[str, str],
    body_text: Optional[str],
    secret_values: Optional[list[str]] = None,
) -> tuple[str, dict[str, str], Optional[str]]:
    """对片段输入打码：敏感请求头 + 文本中的 secret 值。"""
    from core.api_runner.variables import mask_headers, mask_secrets_in_text

    secrets = [s for s in (secret_values or []) if s]
    masked_headers = mask_headers(dict(headers or {}))
    masked_url = mask_secrets_in_text(url, secrets)
    masked_body = mask_secrets_in_text(body_text or "", secrets) if body_text else body_text
    return masked_url, masked_headers, masked_body
