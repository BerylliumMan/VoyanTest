"""HAR 导入：只生成接口定义，不生成用例。"""
from __future__ import annotations

from urllib.parse import urlparse, parse_qsl

from .common import ParsedDocument, ParsedOperation


def parse_har(doc: dict) -> ParsedDocument:
    entries = ((doc.get("log") or {}).get("entries") or []) if isinstance(doc, dict) else []
    operations: list[ParsedOperation] = []
    warnings: list[str] = []
    seen: set[tuple[str, str]] = set()
    for index, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            continue
        request = entry.get("request") if isinstance(entry.get("request"), dict) else {}
        method = str(request.get("method") or "GET").upper()
        raw_url = str(request.get("url") or "").strip()
        if not raw_url:
            warnings.append(f"第 {index} 条 HAR 记录没有 URL，已跳过")
            continue
        parsed = urlparse(raw_url)
        path = parsed.path or "/"
        key = (method, path)
        if key in seen:
            continue
        seen.add(key)
        query = [{"name": k, "in": "query", "type": "string", "example": v} for k, v in parse_qsl(parsed.query)]
        headers = request.get("headers") if isinstance(request.get("headers"), list) else []
        header_params = [
            {"name": str(h.get("name")), "in": "header", "type": "string", "example": h.get("value")}
            for h in headers
            if isinstance(h, dict) and h.get("name")
        ]
        post = request.get("postData") if isinstance(request.get("postData"), dict) else {}
        body = {
            "content_type": str(post.get("mimeType") or ""),
            "example": post.get("text") or "",
        }
        operations.append(
            ParsedOperation(
                name=f"{method} {path}",
                method=method,
                path=path,
                summary=raw_url,
                request_schema={"params": query + header_params, "body": body},
                response_schema={},
            )
        )
    if not operations:
        warnings.append("HAR 中没有可导入的请求")
    return ParsedDocument(source="har", operations=operations, warnings=warnings)
