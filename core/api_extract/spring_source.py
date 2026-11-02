"""046：Spring 源码接口提取器（tree-sitter-java）。

覆盖：
- @RestController / @Controller 类
- 类级 @RequestMapping + 方法级 @GetMapping/@PostMapping/@PutMapping/@DeleteMapping/@PatchMapping/@RequestMapping
- 同文件 `static final String` 常量拼接
- 无法解析的表达式 → confidence=low + 原因（不编造路径）
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from core.api_extract.base import (
    CONFIDENCE_HIGH,
    CONFIDENCE_LOW,
    ExtractContext,
    ExtractedApi,
    ExtractorError,
)

logger = logging.getLogger(__name__)

_CONTROLLER_ANNOTATIONS = {"RestController", "Controller"}
_METHOD_ANNOTATIONS = {
    "GetMapping": "GET",
    "PostMapping": "POST",
    "PutMapping": "PUT",
    "DeleteMapping": "DELETE",
    "PatchMapping": "PATCH",
}
_REQUEST_MAPPING = "RequestMapping"
_SKIP_ANNOTATIONS = {"Deprecated", "Hidden"}
_CONST_RE = re.compile(
    r"\b(?:static\s+final|final\s+static)\s+String\s+([A-Za-z_][A-Za-z0-9_]*)\s*=\s*\"([^\"]*)\""
)
_METHOD_ATTR_RE = re.compile(r"RequestMethod\.(GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS)")
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")
# 046 v2：不解析构建产物与依赖目录，避免大仓库扫描爆炸
_IGNORED_SEGMENTS = {
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
}


def _is_ignored(rel_path: str) -> bool:
    return any(part in _IGNORED_SEGMENTS for part in rel_path.split("/")[:-1])


class SpringSourceExtractor:
    """解析 Java 源码中的 Spring MVC 接口声明。"""

    name = "spring_source"

    def extract(self, ctx: ExtractContext) -> list[ExtractedApi]:
        repo_dir = ctx.repo_dir
        if not repo_dir or not Path(repo_dir).is_dir():
            raise ExtractorError("缺少源码目录（repo_dir）")
        try:
            from tree_sitter import Language, Parser
            import tree_sitter_java
        except Exception as exc:  # pragma: no cover - 依赖缺失
            raise ExtractorError(f"tree-sitter 不可用：{exc}") from exc

        lang = Language(tree_sitter_java.language())
        parser = Parser(lang)
        base = Path(repo_dir)
        roots = ctx.source_roots or ([ctx.source_root] if ctx.source_root else [None])

        # 046 v2：增量模式只解析变更文件；并支持取消与扫描上限
        changed = {
            str(p).replace("\\", "/").lstrip("./")
            for p in (ctx.changed_files or [])
        }
        incremental = bool(changed)
        max_files = int(ctx.max_files or 0)

        results: list[ExtractedApi] = []
        seen: set[str] = set()
        scanned = 0
        truncated = False
        for root_name in roots:
            scan_root = base / (root_name or "")
            if not scan_root.is_dir():
                continue
            for java_file in sorted(scan_root.rglob("*.java")):
                if ctx.should_cancel is not None and ctx.should_cancel():
                    logger.info("提取被取消（已扫描 %s 个文件）", scanned)
                    return results
                rel = str(java_file.relative_to(base)).replace("\\", "/")
                if rel in seen:
                    continue
                if _is_ignored(rel):
                    continue
                if incremental and rel not in changed:
                    continue
                seen.add(rel)
                if max_files and scanned >= max_files:
                    truncated = True
                    logger.warning("扫描达到上限 %s，后续文件被跳过", max_files)
                    break
                scanned += 1
                try:
                    src = java_file.read_bytes()
                    tree = parser.parse(src)
                    results.extend(_extract_file(tree.root_node, src, rel))
                except Exception:
                    logger.debug("解析 %s 失败", java_file, exc_info=True)
            if truncated:
                break
        if truncated:
            logger.warning("spring_source 扫描被上限截断：max_files=%s", max_files)
        return results


def _text(src: bytes, node) -> str:
    return src[node.start_byte:node.end_byte].decode("utf-8", "replace")


def _unquote(raw: str) -> str:
    s = raw.strip()
    if len(s) >= 2 and s[0] == s[-1] == '"':
        s = s[1:-1]
    return s.replace('\\"', '"')


def _annotation_arg_exprs(src: bytes, node) -> list[str]:
    """返回注解的语义参数原始表达式（保留引号），供 `_resolve_path` 解析。

    - `@GetMapping("/x")` → ['"/x"']
    - `@PostMapping(value = "/x", produces = "json")` → ['"/x"']（忽略非 value 属性）
    - `@RequestMapping(Paths.BASE)` → ['Paths.BASE']（无法解析的常量）
    """
    ann_list = next((c for c in node.children if c.type == "annotation_argument_list"), None)
    if ann_list is None:
        return []
    exprs: list[str] = []
    for a in ann_list.children:
        if a.type in {"(", ")", ",", "{"}:
            continue
        if a.type == "element_value_pair":
            key = next((_text(src, c) for c in a.children if c.type == "identifier"), None)
            if key != "value":
                continue
            val = next(
                (c for c in a.children if c.type not in {"identifier", "="}), None
            )
            if val is not None:
                exprs.append(_text(src, val))
        elif a.type in {"array_initializer", "element_value_array_initializer"}:
            exprs.extend(
                _text(src, c) for c in a.children if c.type == "string_literal"
            )
        else:
            exprs.append(_text(src, a))
    return exprs


def _annotations(src: bytes, node) -> list[tuple[str, list[str], str]]:
    """返回 [(注解名, 参数表达式列表, 原始文本)]。

    参数保留原始表达式（含引号 / 常量名），由 `_resolve_path` 判定是否可解析。
    """
    out: list[tuple[str, list[str], str]] = []
    mods = next((c for c in node.children if c.type == "modifiers"), None)
    if mods is None:
        return out
    for ch in mods.children:
        if ch.type == "marker_annotation":
            # 无参数注解（@RestController / @Deprecated 等）
            ident = next((c for c in ch.children if c.type == "identifier"), None)
            if ident is not None:
                out.append((_text(src, ident), [], _text(src, ch)))
        elif ch.type == "annotation":
            ident = next((c for c in ch.children if c.type == "identifier"), None)
            if ident is not None:
                out.append((_text(src, ident), _annotation_arg_exprs(src, ch), _text(src, ch)))
    return out


def _split_plus(s: str) -> list[str]:
    """按顶层 `+` 拆分（忽略字符串字面量内部的 +）。"""
    parts: list[str] = []
    buf: list[str] = []
    in_str = False
    escaped = False
    for ch in s:
        if escaped:
            buf.append(ch)
            escaped = False
            continue
        if ch == "\\" and in_str:
            buf.append(ch)
            escaped = True
            continue
        if ch == '"':
            in_str = not in_str
        elif ch == "+" and not in_str:
            parts.append("".join(buf))
            buf = []
            continue
        buf.append(ch)
    parts.append("".join(buf))
    return parts


def _resolve_path(raw: str, consts: dict[str, str]) -> tuple[str | None, str | None]:
    """把注解参数表达式解析成路径；无法确定返回 (None, 原因)。"""
    s = (raw or "").strip()
    if not s:
        return "", None
    out: list[str] = []
    unknown: list[str] = []
    for part in _split_plus(s):
        part = part.strip()
        if not part:
            continue
        quoted = re.findall(r'"([^"]*)"', part)
        if quoted:
            out.append("".join(quoted))
        elif _IDENT_RE.match(part):
            # 常量 / 全限定名引用
            if part in consts:
                out.append(consts[part])
            else:
                unknown.append(part)
        else:
            # 本身即是路径字面量（如 "/api/users"）
            out.append(part)
    if unknown:
        return None, f"无法解析的常量：{', '.join(unknown[:3])}"
    return "".join(out), None


def _join_path(base: str, sub: str) -> str:
    parts = [p.strip("/") for p in (base or "", sub or "") if p.strip("/")]
    return "/" + "/".join(parts) if parts else "/"


def _extract_file(root, src: bytes, rel_path: str) -> list[ExtractedApi]:
    consts = {m.group(1): m.group(2) for m in _CONST_RE.finditer(src.decode("utf-8", "replace"))}
    out: list[ExtractedApi] = []

    def visit(node, class_base: str = "", in_controller: bool = False) -> None:
        if node.type == "class_declaration":
            anns = _annotations(src, node)
            names = {a[0] for a in anns}
            is_controller = bool(names & _CONTROLLER_ANNOTATIONS) or in_controller
            base = class_base
            low_reason = None
            for name, args, _raw in anns:
                if name == _REQUEST_MAPPING and args:
                    resolved, err = _resolve_path(args[0], consts)
                    if resolved is None:
                        low_reason = err
                    else:
                        base = _join_path(class_base, resolved)
            class_name = next(
                (_text(src, c) for c in node.children if c.type == "identifier"), ""
            )
            for child in node.children:
                if child.type == "class_body":
                    for member in child.children:
                        if member.type == "method_declaration":
                            out.extend(
                                _method_apis(
                                    src, member, base, is_controller, rel_path,
                                    class_name, consts, low_reason,
                                )
                            )
                        elif member.type == "class_declaration":
                            visit(member, base, is_controller)
        else:
            for child in node.children:
                visit(child, class_base, in_controller)

    visit(root)
    return out


def _method_apis(
    src: bytes, node, class_base: str, in_controller: bool, rel_path: str,
    class_name: str, consts: dict[str, str], class_low_reason: str | None,
) -> list[ExtractedApi]:
    if not in_controller:
        return []
    anns = _annotations(src, node)
    if not anns:
        return []
    if {a[0] for a in anns} & _SKIP_ANNOTATIONS:
        return []
    method_name = next((_text(src, c) for c in node.children if c.type == "identifier"), "")
    line = node.start_point[0] + 1
    out: list[ExtractedApi] = []

    for ann_name, args, raw_ann in anns:
        http_method: str | None = _METHOD_ANNOTATIONS.get(ann_name)
        low_reason = class_low_reason
        sub_path = ""
        if ann_name == _REQUEST_MAPPING:
            m = _METHOD_ATTR_RE.search(raw_ann)
            http_method = m.group(1) if m else None
            if http_method is None:
                low_reason = low_reason or "未声明 HTTP 方法"
            if args:
                resolved, err = _resolve_path(args[0], consts)
                if resolved is None:
                    low_reason = err
                else:
                    sub_path = resolved
        elif http_method:
            if args:
                resolved, err = _resolve_path(args[0], consts)
                if resolved is None:
                    low_reason = err
                else:
                    sub_path = resolved
        else:
            continue

        path = _join_path(class_base, sub_path)
        confidence = CONFIDENCE_HIGH if http_method and not low_reason else CONFIDENCE_LOW
        params = _params(src, node)
        out.append(ExtractedApi(
            method=http_method or "GET",
            path=path,
            summary=f"{class_name}.{method_name}" if class_name else method_name,
            request_schema={"params": params} if params else {},
            source_file=rel_path,
            source_line=line,
            source_symbol=f"{class_name}.{method_name}" if class_name else method_name,
            extractor="spring_source",
            confidence=confidence,
            confidence_reason=low_reason,
        ).normalized())
    return out


def _params(src: bytes, node) -> list[dict]:
    """从方法形参上提取 @RequestParam / @PathVariable 名称。"""
    params: list[dict] = []
    for child in node.children:
        if child.type != "formal_parameters":
            continue
        for p in child.children:
            if p.type != "formal_parameter":
                continue
            anns = _annotations(src, p)
            for name, args, _raw in anns:
                if name not in ("RequestParam", "PathVariable"):
                    continue
                if args:
                    pname = _unquote(args[0])
                else:
                    ident = next((c for c in p.children if c.type == "identifier"), None)
                    pname = _text(src, ident) if ident else ""
                if pname:
                    params.append({
                        "name": pname,
                        "in": "path" if name == "PathVariable" else "query",
                    })
    return params
