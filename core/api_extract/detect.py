"""046：仓库技术栈探测。

用于在同步时决定走哪个提取器、以及从哪些源码目录扫描。
只做轻量文本/文件特征判断，不编译、不执行。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# 这些目录里的内容不代表仓库源码
_IGNORED_DIRS = {
    "target",
    "build",
    "out",
    "node_modules",
    ".git",
    ".gradle",
    ".idea",
    ".mvn",
    "dist",
    "coverage",
    "generated",
    "generated-sources",
    "vendor",
}

_SPRING_WEB_RE = (
    "spring-boot-starter-web",
    "spring-webmvc",
    "spring-mvc",
    "org.springframework.boot",
)
_CONTROLLER_RE = ("@RestController", "@Controller")
_OPENAPI_NAMES = {
    "openapi.yaml",
    "openapi.yml",
    "openapi.json",
    "swagger.yaml",
    "swagger.yml",
    "swagger.json",
    "api-docs.json",
}

_MAX_SCAN_FILES = 2000
_MAX_READ_BYTES = 1_000_000


@dataclass
class TechStack:
    """仓库技术栈探测结果。"""

    spring: bool = False
    openapi: bool = False
    build_tool: str | None = None
    # 相对仓库根目录的源码目录（如 src/main/java、user-service/src/main/java）
    source_roots: list[str] = field(default_factory=list)
    # 相对仓库根目录的 OpenAPI 文档
    openapi_paths: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)

    def extractor_names(self) -> list[str]:
        """按优先级返回可用的提取器名称（文档优先，源码兜底）。"""
        names: list[str] = []
        if self.openapi:
            names.append("openapi")
        if self.spring:
            names.append("spring_source")
        return names


def detect_tech_stack(
    repo_dir: str | None, *, max_files: int = _MAX_SCAN_FILES
) -> TechStack:
    """探测仓库技术栈；目录不存在时返回空结果（不抛异常）。"""
    stack = TechStack()
    if not repo_dir:
        return stack
    root = Path(repo_dir)
    if not root.is_dir():
        return stack

    _detect_build(root, stack, max_files=max_files)
    _detect_openapi(root, stack, max_files=max_files)
    stack.source_roots = _find_source_roots(root)
    if not stack.spring:
        _detect_spring_annotations(root, stack, max_files=max_files)
    if stack.source_roots and "source_roots" not in stack.evidence:
        stack.evidence.append(f"source_roots:{','.join(stack.source_roots)}")
    return stack


def _iter_files(root: Path, limit: int = _MAX_SCAN_FILES):
    """遍历仓库文件，跳过构建产物与依赖目录。"""
    count = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            d for d in dirnames if d not in _IGNORED_DIRS and not d.startswith(".")
        )
        for name in sorted(filenames):
            if count >= limit:
                return
            count += 1
            yield Path(dirpath) / name


def _read_text(path: Path) -> str:
    try:
        with path.open("rb") as fh:
            raw = fh.read(_MAX_READ_BYTES)
    except OSError:
        return ""
    return raw.decode("utf-8", "ignore")


def _detect_build(root: Path, stack: TechStack, *, max_files: int = _MAX_SCAN_FILES) -> None:
    for path in _iter_files(root, limit=max_files):
        name = path.name
        if name == "pom.xml":
            text = _read_text(path)
            if any(sig in text for sig in _SPRING_WEB_RE):
                stack.spring = True
                stack.evidence.append(f"maven:{_rel(root, path)}")
            if stack.build_tool is None:
                stack.build_tool = "maven"
        elif name in ("build.gradle", "build.gradle.kts"):
            text = _read_text(path)
            if any(sig in text for sig in _SPRING_WEB_RE):
                stack.spring = True
                stack.evidence.append(f"gradle:{_rel(root, path)}")
            if stack.build_tool is None:
                stack.build_tool = "gradle"


def _detect_openapi(root: Path, stack: TechStack, *, max_files: int = _MAX_SCAN_FILES) -> None:
    found: list[str] = []
    for path in _iter_files(root, limit=max_files):
        if path.name.lower() in _OPENAPI_NAMES:
            found.append(_rel(root, path))
    if found:
        stack.openapi = True
        stack.openapi_paths = sorted(found)
        stack.evidence.append("openapi:" + ",".join(stack.openapi_paths[:3]))


def _find_source_roots(root: Path) -> list[str]:
    """识别 Maven/Gradle 的 Java 源码目录（支持多模块）。"""
    roots: list[str] = []
    # 根目录
    if (root / "src" / "main" / "java").is_dir():
        roots.append("src/main/java")
    # 一级子模块
    for child in sorted(root.iterdir()):
        if not child.is_dir() or child.name in _IGNORED_DIRS or child.name.startswith("."):
            continue
        java_dir = child / "src" / "main" / "java"
        if java_dir.is_dir():
            roots.append(f"{child.name}/src/main/java")
            continue
        # 二级模块（聚合工程里常见的 group/module 结构）
        for sub in sorted(child.iterdir()):
            if not sub.is_dir() or sub.name in _IGNORED_DIRS or sub.name.startswith("."):
                continue
            sub_java = sub / "src" / "main" / "java"
            if sub_java.is_dir():
                roots.append(f"{child.name}/{sub.name}/src/main/java")
    return roots


def _detect_spring_annotations(
    root: Path, stack: TechStack, *, max_files: int = _MAX_SCAN_FILES
) -> None:
    """没有构建文件时，用源码注解特征兜底判断。"""
    scanned = 0
    for path in _iter_files(root, limit=max_files):
        if path.suffix != ".java":
            continue
        scanned += 1
        if scanned > max_files:
            break
        text = _read_text(path)
        if any(sig in text for sig in _CONTROLLER_RE) and "@RequestMapping" in text:
            stack.spring = True
            stack.evidence.append(f"annotation:{_rel(root, path)}")
            return


def _rel(root: Path, path: Path) -> str:
    try:
        return str(path.relative_to(root)).replace("\\", "/")
    except ValueError:
        return str(path)
