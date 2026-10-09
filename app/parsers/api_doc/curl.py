"""033（US11）：cURL 命令解析器（粘贴导入，零出站）。

支持范围（契约 C7）：-X/--request、-H/--header、-d/--data*/--json、-F/--form（含 @文件）、
-u/--user（basic）、--url、-G/--get、常见噪声旗标（-s/-k/-L/--compressed/-A 等）、
行续接与单双引号转义。失败抛 ``CurlParseError``（路由层转 400 可读错误）。
"""

from __future__ import annotations

import json
import re
import shlex
from typing import Optional

__all__ = ["CurlParseError", "parse_curl"]

_JSON_RE = re.compile(r"^\s*[\[{]")


class CurlParseError(ValueError):
    """cURL 文本不可解析（调用方转 400）。"""


_NOISE_FLAGS = {
    "-s", "--silent", "-k", "--insecure", "-i", "--include", "-v", "--verbose",
    "-L", "--location", "--compressed", "-#", "--progress-bar", "-f", "--fail",
    "--no-buffer", "-N",
}
_NOISE_VALUE_FLAGS = {"-o", "--output", "-w", "--write-out"}


def _tokenize(text: str) -> list[str]:
    """行续接清理 + shlex 分词（posix 语义：单双引号与转义）。"""
    cleaned = re.sub(r"\\\r?\n", " ", text)
    try:
        return shlex.split(cleaned, posix=True)
    except ValueError as exc:
        raise CurlParseError(f"cURL 引号不闭合或转义非法: {exc}") from exc


def parse_curl(text: str) -> dict:
    """把 cURL 命令文本解析为编辑器可用的 api_spec 步骤（order=1）。"""
    if not isinstance(text, str) or not text.strip():
        raise CurlParseError("cURL 内容为空")

    tokens = _tokenize(text.strip())
    # 允许行首 $ / > 提示符与 curl 可执行名
    while tokens and tokens[0] in ("$", ">", "PS>", "\\"):
        tokens.pop(0)
    if not tokens:
        raise CurlParseError("cURL 内容为空")
    if tokens[0].lower() not in ("curl", "curl.exe"):
        raise CurlParseError(f"不是 curl 命令（以 {tokens[0]!r} 开头）")
    tokens = tokens[1:]

    method: Optional[str] = None
    url: Optional[str] = None
    headers: list[dict] = []
    data_parts: list[str] = []
    form_fields: list[dict] = []
    files: list[dict] = []
    basic: Optional[dict] = None
    force_get = False
    explicit_json = False

    i = 0
    while i < len(tokens):
        tok = tokens[i]
        low = tok.lower()

        def _next(flag: str) -> str:
            nonlocal i
            if i + 1 >= len(tokens):
                raise CurlParseError(f"{flag} 缺少取值")
            i += 1
            return tokens[i]

        if tok in _NOISE_FLAGS:
            i += 1
            continue
        if low in _NOISE_VALUE_FLAGS:
            _next(tok)
            i += 1
            continue
        if low in ("-x", "--request"):
            method = _next(tok).upper()
            i += 1
            continue
        if low in ("-h", "--header"):
            raw = _next(tok)
            if ":" in raw:
                key, value = raw.split(":", 1)
            elif ";" in raw:  # 旧式 "K;V"
                key, value = raw.split(";", 1)
            else:
                key, value = raw, ""
            headers.append({"key": key.strip(), "value": value.strip(), "enable": True})
            i += 1
            continue
        if low in ("-d", "--data", "--data-raw", "--data-binary", "--data-urlencode"):
            data_parts.append(_next(tok))
            i += 1
            continue
        if low == "--json":
            data_parts.append(_next(tok))
            explicit_json = True
            i += 1
            continue
        if low in ("-f", "--form") and tok != "-f":  # -f 已作为噪声旗标（--fail）
            raw = _next(tok)
            if "=" not in raw:
                raise CurlParseError(f"表单字段缺少 '=': {raw!r}")
            key, value = raw.split("=", 1)
            if value.startswith("@"):
                files.append({"key": key, "path": value[1:]})
            else:
                form_fields.append({"key": key, "value": value, "enable": True})
            i += 1
            continue
        if low in ("-u", "--user"):
            raw = _next(tok)
            username, _, password = raw.partition(":")
            basic = {"type": "basic", "username": username, "password": password}
            i += 1
            continue
        if low in ("-a", "--user-agent"):
            headers.append({"key": "User-Agent", "value": _next(tok), "enable": True})
            i += 1
            continue
        if low == "--url":
            url = _next(tok)
            i += 1
            continue
        if low in ("-g", "--get"):
            force_get = True
            i += 1
            continue
        if tok.startswith("-") and tok != "-":
            # 未知旗标：保守拒绝（避免误吞 URL）
            raise CurlParseError(f"不支持的 cURL 参数: {tok!r}")
        if url is None:
            url = tok
            i += 1
            continue
        # 多个裸参数：忽略（可能是 -o 残留等），保守报错更安全
        raise CurlParseError(f"无法识别的参数: {tok!r}")

    if not url:
        raise CurlParseError("缺少 URL")

    # 方法推断：显式 > -G > 有数据/表单 → POST > GET
    if method is None:
        method = "GET" if force_get or not (data_parts or form_fields or files) else "POST"

    body: dict = {"type": "none", "content": "", "form_data": [], "files": []}
    if form_fields or files:
        body["type"] = "form_data"
        body["form_data"] = form_fields
        body["files"] = files
    elif data_parts:
        content = "&".join(data_parts)
        content_type = ""
        for h in headers:
            if h["key"].lower() == "content-type":
                content_type = h["value"].lower()
        if explicit_json or "json" in content_type or _JSON_RE.match(content):
            body["type"] = "json"
            if content_type and "json" not in content_type:
                pass  # 用户显式头为准，不改写
            elif not content_type and explicit_json:
                headers.append({"key": "Content-Type", "value": "application/json", "enable": True})
        elif "x-www-form-urlencoded" in content_type or all("=" in p for p in data_parts):
            body["type"] = "form"
        else:
            body["type"] = "raw"
        body["content"] = content
        if body["type"] == "json":
            try:
                json.loads(content)
            except ValueError:
                pass  # 含变量/不合法 JSON 时保留原文（编辑器可提示）

    return {
        "order": 1,
        "name": "",
        "enable": True,
        "request": {
            "method": method,
            "url": url,
            "headers": headers,
            "query": [],
            "body": body,
            "auth": basic or {"type": "none"},
        },
        "assertions": [],
        "extractors": [],
    }
