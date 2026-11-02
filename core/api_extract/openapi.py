"""046：OpenAPI / Swagger2 提取器（复用 029 的文档解析器）。"""

from __future__ import annotations

import logging

from core.api_extract.base import (
    CONFIDENCE_HIGH,
    ExtractContext,
    ExtractedApi,
    ExtractorError,
)

logger = logging.getLogger(__name__)


class OpenApiExtractor:
    """从 OpenAPI3 / Swagger2 文档提取接口。"""

    name = "openapi"

    def extract(self, ctx: ExtractContext) -> list[ExtractedApi]:
        doc = ctx.openapi_doc
        if doc is None and ctx.openapi_url:
            doc = _fetch_doc(ctx.openapi_url)
        if not isinstance(doc, dict):
            raise ExtractorError("缺少 OpenAPI 文档：请提供文档内容或可访问的地址")

        from app.parsers.api_doc.openapi import parse_openapi

        parsed = parse_openapi(doc)
        out: list[ExtractedApi] = []
        for op in parsed.operations:
            out.append(ExtractedApi(
                method=op.method,
                path=op.path,
                summary=op.summary or "",
                operation_id=op.operation_id or None,
                request_schema=op.request_schema or {},
                response_schema=op.response_schema or None,
                tags=op.tags or None,
                extractor=self.name,
                confidence=CONFIDENCE_HIGH,
            ).normalized())
        if parsed.warnings:
            logger.info("openapi 提取告警 %d 条：%s", len(parsed.warnings), parsed.warnings[:3])
        return out


def _fetch_doc(url: str) -> dict:
    """拉取 OpenAPI 文档（JSON/YAML）。失败抛 ExtractorError。"""
    import httpx

    from app.parsers.api_doc.common import _load_json_or_yaml

    try:
        resp = httpx.get(url, timeout=20, follow_redirects=True)
        resp.raise_for_status()
    except Exception as exc:
        raise ExtractorError(f"拉取 OpenAPI 文档失败：{exc}") from exc
    try:
        doc = _load_json_or_yaml(resp.content)
    except Exception as exc:
        raise ExtractorError(f"OpenAPI 文档解析失败：{exc}") from exc
    if not isinstance(doc, dict):
        raise ExtractorError("OpenAPI 文档不是对象")
    return doc
