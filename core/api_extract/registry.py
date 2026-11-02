"""046：提取器注册表与选择策略。"""

from __future__ import annotations

from core.api_extract.base import Extractor, ExtractorError
from core.api_extract.detect import TechStack
from core.api_extract.openapi import OpenApiExtractor
from core.api_extract.spring_source import SpringSourceExtractor

# 策略名 → 提取器工厂
_REGISTRY: dict[str, type] = {
    "openapi": OpenApiExtractor,
    "spring_source": SpringSourceExtractor,
}

SELECTOR_AUTO = "auto"
SELECTORS = (SELECTOR_AUTO, *_REGISTRY.keys())


def available_extractors() -> list[str]:
    return list(_REGISTRY)


def get_extractor(name: str) -> Extractor:
    """按名称实例化提取器；未知名称抛 ExtractorError。"""
    factory = _REGISTRY.get((name or "").strip().lower())
    if factory is None:
        raise ExtractorError(f"未知提取器：{name}（可选：{', '.join(_REGISTRY)}）")
    return factory()  # type: ignore[return-value]


def resolve_extractors(
    preference: str | None,
    stack: TechStack,
) -> list[Extractor]:
    """根据偏好与技术栈探测结果给出本次要运行的提取器。

    - `auto`（默认）：按技术栈优先级，OpenAPI 文档优先，Spring 源码兜底
    - 指定名称：只运行该提取器（缺少对应输入时由提取器自行报错）
    """
    choice = (preference or SELECTOR_AUTO).strip().lower()
    if choice == SELECTOR_AUTO:
        return [get_extractor(name) for name in stack.extractor_names()]
    return [get_extractor(choice)]
