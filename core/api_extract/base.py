"""046：接口提取器统一契约。

所有提取器（OpenAPI / Spring 源码 / 后续 JAX-RS 等）都产出一致的 `ExtractedApi`，
由同步服务按 (method, path) 合并进 api_definitions。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Protocol, runtime_checkable

CONFIDENCE_HIGH = "high"
CONFIDENCE_MEDIUM = "medium"
CONFIDENCE_LOW = "low"
CONFIDENCES = (CONFIDENCE_HIGH, CONFIDENCE_MEDIUM, CONFIDENCE_LOW)


@dataclass
class ExtractedApi:
    """一个接口的提取结果。"""

    method: str
    path: str
    summary: str = ""
    operation_id: str | None = None
    request_schema: dict = field(default_factory=dict)
    response_schema: dict | None = None
    tags: str | None = None
    source_file: str | None = None
    source_line: int | None = None
    source_symbol: str | None = None
    extractor: str = "openapi"
    confidence: str = CONFIDENCE_HIGH
    confidence_reason: str | None = None

    def normalized(self) -> "ExtractedApi":
        """规范化 method 大写、path 前导斜杠；便于与旧数据按唯一键对齐。"""
        method = (self.method or "").strip().upper()
        path = "/" + (self.path or "").strip().lstrip("/")
        if path != "/":
            path = path.rstrip("/")
        return ExtractedApi(
            method=method,
            path=path,
            summary=self.summary or "",
            operation_id=self.operation_id,
            request_schema=self.request_schema or {},
            response_schema=self.response_schema,
            tags=self.tags,
            source_file=self.source_file,
            source_line=self.source_line,
            source_symbol=self.source_symbol,
            extractor=self.extractor,
            confidence=self.confidence if self.confidence in CONFIDENCES else CONFIDENCE_LOW,
            confidence_reason=self.confidence_reason,
        )


@dataclass
class ExtractContext:
    """提取所需的输入（不同提取器按需取用）。"""

    # 本地仓库目录（源码解析用）
    repo_dir: str | None = None
    # 源码子目录（相对 repo_dir）
    source_root: str | None = None
    # 多模块源码目录（相对 repo_dir）；非空时优先于 source_root
    source_roots: list[str] | None = None
    # 已解析的 OpenAPI/Swagger 文档（字典）
    openapi_doc: dict | None = None
    # OpenAPI 地址（文档未预加载时由提取器自行获取）
    openapi_url: str | None = None
    # 046 v2：增量同步时只解析这些仓库相对路径（None/空 = 全量）
    changed_files: list[str] | None = None
    # 046 v2：协作式取消检查（提取器应在文件粒度调用）
    should_cancel: Callable[[], bool] | None = None
    # 046 v2：单次扫描文件上限（0/None = 不限制）
    max_files: int | None = None


@runtime_checkable
class Extractor(Protocol):
    """提取器协议：name 用于配置与记录，extract 必须容错（解析失败抛 ExtractorError）。"""

    name: str

    def extract(self, ctx: ExtractContext) -> list[ExtractedApi]: ...


class ExtractorError(RuntimeError):
    """提取失败（用于区分"没有接口"与"解析出错"）。"""
