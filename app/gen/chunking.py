"""Chapter-aware Phase-1 document chunking.

Strategy:
1. Prefer splitting on chapter / section headings (and file headers).
2. If a single chapter still exceeds the token budget (~80% context), split
   inside that chapter by paragraphs / character windows.
3. Keep images with their preceding text when packing.
4. Label continuations as ``模块「X」第 i/n 段`` and optionally prepend a short
   bridge from the previous sub-chunk.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# Heuristic: Chinese / mixed docs ≈ 1.5 tokens per character.
CHARS_PER_TOKEN_INV = 1.5
# Rough vision cost per embedded image (base64 size varies widely).
IMAGE_TOKEN_COST = 1500
# Reserve for system prompt + model output so the chunk itself fits.
RESERVED_PROMPT_OUTPUT_TOKENS = 6000
# Hard floor so tiny contexts still get a usable window.
MIN_CHUNK_BUDGET = 2000
# Characters of previous sub-chunk tail to carry into the next (same chapter).
BRIDGE_CHARS = 400

_HEADING_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"^#{1,4}\s+(.+)$"),
    # File separators (===== 文件N: name =====) are NOT module headings — handled separately.
    re.compile(r"^第[一二三四五六七八九十百千零〇0-9]+[章节篇部分]\s*[、.．:]?\s*(.*)$"),
    re.compile(r"^[（(]?[一二三四五六七八九十]+[）)]\s*[、.．]?\s*(.+)$"),
    re.compile(r"^\d+(?:\.\d+){0,3}\s+([\u4e00-\u9fffA-Za-z].{0,60})$"),
    re.compile(r"^【([^】]{1,40})】\s*(.*)$"),
]

_FILE_HEADER_RE = re.compile(r"^=====\s*文件\d+(?:\s*[:：].*?)?\s*=====\s*$")
_PAGE_HEADER_RE = re.compile(r"^=====\s*(?:.+?\s*)?第\d+页\s*=====\s*$")
_FILENAME_MODULE_RE = re.compile(
    r"(?i)(?:^文件\d+\b|\.(?:docx?|pdf|md|markdown|png|jpe?g|gif|webp|xlsx?|csv|txt)\s*$)"
)



@dataclass
class Phase1Chunk:
    """One model call unit for Phase-1 extraction."""

    parts: list[dict[str, Any]] = field(default_factory=list)
    text: str = ""
    module: str = "通用"
    segment: int = 1
    segment_total: int = 1
    multimodal: bool = False

    @property
    def intro(self) -> str:
        if self.segment_total <= 1 and self.module in ("通用", "文档开头", ""):
            if self.multimodal:
                return (
                    "请分析以下需求文档内容（文字与图片按文档顺序排列）；"
                    "module 请根据界面/业务功能命名，禁止使用文件名："
                )
            return "请分析以下需求文档，提取测试项："
        mod = self.module or "通用"
        cont = "，续篇" if self.segment > 1 else ""
        base = (
            f"请分析以下需求文档（模块「{mod}」第 {self.segment}/{self.segment_total} 段{cont}；"
            f"只提取本段出现的测试项，module 字段请优先使用「{mod}」"
        )
        if self.multimodal:
            return base + "；文字与图片按文档顺序排列）："
        return base + "）："


def estimate_text_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, int(len(text) * CHARS_PER_TOKEN_INV))


def estimate_part_tokens(part: dict[str, Any]) -> int:
    ptype = part.get("type")
    if ptype == "image":
        return IMAGE_TOKEN_COST
    return estimate_text_tokens(part.get("text") or "")


def estimate_parts_tokens(parts: list[dict[str, Any]]) -> int:
    return sum(estimate_part_tokens(p) for p in parts)


def chunk_token_budget(max_context_tokens: int, ratio: float = 0.8) -> int:
    """Usable input budget per request: ratio of context minus reserved headroom."""
    raw = int(max(1, max_context_tokens) * ratio) - RESERVED_PROMPT_OUTPUT_TOKENS
    return max(MIN_CHUNK_BUDGET, raw)


def _clean_heading_title(raw: str) -> str:
    title = (raw or "").strip()
    title = re.sub(r"^#+\s*", "", title)
    title = re.sub(r"[（(].*?[）)]\s*$", "", title).strip()
    title = title.replace("模块", "").strip() or title
    if len(title) > 40:
        title = title[:40].rstrip()
    return title or "通用"


def is_file_separator_line(line: str) -> bool:
    """True for multi-file / PDF page separator banners (not business modules)."""
    s = (line or "").strip()
    if not s or "\n" in s:
        return False
    return bool(_FILE_HEADER_RE.match(s) or _PAGE_HEADER_RE.match(s))


def looks_like_filename_module(module: str) -> bool:
    """True when module looks like an upload filename / file banner, not a业务模块."""
    m = (module or "").strip()
    if not m:
        return False
    if is_file_separator_line(m):
        return True
    if _FILENAME_MODULE_RE.search(m):
        return True
    # e.g. "需求说明_v1.docx" or "文件1: foo.png"
    if re.search(r"(?i)\.(?:docx?|pdf|md|png|jpe?g)\b", m):
        return True
    return False


MODULE_PATH_SEP = "——"
_MODULE_SEP_SPLIT_RE = re.compile(
    r"\s*(?:——+|—+|–+|-+|/+|\\+|>+|·+|＋+|＋)\s*"
)


def sanitize_module_name(module: str, fallback: str = "通用") -> str:
    m = (module or "").strip()
    if not m or m in ("文档开头",):
        return fallback
    if looks_like_filename_module(m):
        return fallback
    return m


def normalize_module_path(
    raw: str,
    chapter_hint: str | None = None,
    *,
    fallback: str = "通用",
) -> str:
    """Normalize module to ``一级`` or ``一级——二级`` (max 2 levels)."""
    m = sanitize_module_name(raw, fallback="")
    hint = sanitize_module_name(chapter_hint or "", fallback="")
    if hint in ("通用", "文档开头"):
        hint = ""

    if not m:
        # Empty model module → fall back to chapter label (single level)
        if not hint:
            return fallback
        hint_parts = [p.strip() for p in _MODULE_SEP_SPLIT_RE.split(hint) if p and p.strip()]
        return MODULE_PATH_SEP.join(hint_parts[:2]) or fallback

    parts = [p.strip() for p in _MODULE_SEP_SPLIT_RE.split(m) if p and p.strip()]
    cleaned: list[str] = []
    for p in parts:
        p2 = sanitize_module_name(p, fallback="")
        if p2 and p2 not in cleaned:
            cleaned.append(p2)
    if not cleaned:
        return hint or fallback
    return MODULE_PATH_SEP.join(cleaned[:2])


def split_module_path(module: str) -> tuple[str, str | None]:
    """Return ``(primary, secondary|None)`` after normalization."""
    path = normalize_module_path(module)
    if MODULE_PATH_SEP in path:
        left, right = path.split(MODULE_PATH_SEP, 1)
        return left.strip() or "通用", (right.strip() or None)
    return path, None


def primary_module_name(module: str) -> str:
    return split_module_path(module)[0]


def detect_heading(line: str) -> str | None:
    """Return module/chapter title if ``line`` looks like a section heading."""
    s = (line or "").strip()
    if not s or len(s) > 80:
        return None
    # Skip obvious body / table lines / upload file banners
    if s.startswith("|") or s.startswith("```"):
        return None
    if is_file_separator_line(s):
        return None
    for pat in _HEADING_PATTERNS:
        m = pat.match(s)
        if not m:
            continue
        # Prefer last capturing group with content
        groups = [g for g in m.groups() if g and str(g).strip()]
        if groups:
            return _clean_heading_title(groups[-1])
        return _clean_heading_title(s)
    return None


def _split_long_text(text: str, budget: int) -> list[str]:
    """Greedy split by paragraphs, then by character window if needed."""
    if estimate_text_tokens(text) <= budget:
        return [text]
    paras = text.split("\n")
    chunks: list[str] = []
    buf: list[str] = []
    buf_tokens = 0

    def flush() -> None:
        nonlocal buf, buf_tokens
        if buf:
            chunks.append("\n".join(buf))
            buf = []
            buf_tokens = 0

    for para in paras:
        pt = estimate_text_tokens(para) + (1 if buf else 0)
        if pt > budget:
            flush()
            max_chars = max(200, int(budget / CHARS_PER_TOKEN_INV))
            for i in range(0, len(para), max_chars):
                piece = para[i : i + max_chars]
                if piece.strip():
                    chunks.append(piece)
            continue
        if buf and buf_tokens + pt > budget:
            flush()
        buf.append(para)
        buf_tokens += pt
    flush()
    return chunks or [text[: max(200, int(budget / CHARS_PER_TOKEN_INV))]]


def split_text_by_headings(text: str) -> list[tuple[str, str]]:
    """Split plain text into ``(module_title, body)`` chapter sections."""
    text = text or ""
    if not text.strip():
        return []

    lines = text.splitlines(keepends=True)
    chapters: list[tuple[str, list[str]]] = []
    current_module = "文档开头"
    buf: list[str] = []

    def flush() -> None:
        nonlocal buf
        body = "".join(buf).strip()
        buf = []
        if body:
            chapters.append((current_module, body))

    for line in lines:
        raw = line.rstrip("\n")
        # File / page banners: structural split only, never a business module
        if is_file_separator_line(raw):
            flush()
            current_module = "通用"
            buf.append(line)
            continue
        heading = detect_heading(raw)
        if heading is not None:
            flush()
            current_module = heading
            buf.append(line)
            continue
        buf.append(line)
    flush()

    if not chapters and text.strip():
        return [("文档开头", text.strip())]
    return [(m, b) for m, b in chapters]


def _bridge_prefix(prev_text: str, module: str) -> str:
    tail = (prev_text or "").strip()
    if not tail:
        return ""
    if len(tail) > BRIDGE_CHARS:
        tail = "…" + tail[-BRIDGE_CHARS:]
    return f"【衔接：模块「{module}」上一子段末尾】\n{tail}\n\n---\n\n"


def _pack_parts_with_image_affinity(
    parts: list[dict[str, Any]],
    budget: int,
) -> list[list[dict[str, Any]]]:
    """Pack parts under budget; keep an image with preceding text when possible."""
    if not parts:
        return []

    chunks: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_tokens = 0

    def flush() -> None:
        nonlocal current, current_tokens
        if current:
            chunks.append(current)
            current = []
            current_tokens = 0

    i = 0
    while i < len(parts):
        part = parts[i]
        ptype = part.get("type")

        if ptype == "text":
            text = part.get("text") or ""
            if not text.strip():
                i += 1
                continue
            pieces = _split_long_text(text, budget)
            for pi, piece in enumerate(pieces):
                pt = estimate_text_tokens(piece)
                # Peek: if next is image and this is the last piece, try to keep them together
                next_img = None
                if pi == len(pieces) - 1 and i + 1 < len(parts) and parts[i + 1].get("type") == "image":
                    next_img = parts[i + 1]
                need = pt + (IMAGE_TOKEN_COST if next_img else 0)

                if current and current_tokens + need > budget:
                    flush()
                if current and current_tokens + pt > budget:
                    flush()

                current.append({"type": "text", "text": piece})
                current_tokens += pt

                if next_img is not None and current_tokens + IMAGE_TOKEN_COST <= budget:
                    current.append(next_img)
                    current_tokens += IMAGE_TOKEN_COST
                    i += 1  # consume image with this text
                    next_img = None

                if current_tokens >= budget:
                    flush()
            i += 1
            continue

        # image without preceding text in this iteration
        pt = estimate_part_tokens(part)
        if current and current_tokens + pt > budget:
            # Prefer moving last short text with the image into next chunk
            if (
                current
                and current[-1].get("type") == "text"
                and estimate_part_tokens(current[-1]) + pt <= budget
            ):
                carry = current.pop()
                carry_tok = estimate_part_tokens(carry)
                current_tokens -= carry_tok
                flush()
                current = [carry, part]
                current_tokens = carry_tok + pt
            else:
                flush()
                current.append(part)
                current_tokens = pt
        else:
            current.append(part)
            current_tokens += pt
        if current_tokens >= budget:
            flush()
        i += 1

    flush()
    return chunks


def _finalize_chapter_chunks(
    module: str,
    packed: list[list[dict[str, Any]]] | list[str],
    *,
    multimodal: bool,
) -> list[Phase1Chunk]:
    total = max(1, len(packed))
    out: list[Phase1Chunk] = []
    prev_plain = ""

    for idx, unit in enumerate(packed, start=1):
        if multimodal:
            parts = list(unit)  # type: ignore[arg-type]
            if idx > 1 and prev_plain:
                bridge = _bridge_prefix(prev_plain, module)
                parts = [{"type": "text", "text": bridge}] + parts
            plain_bits = [
                p.get("text") or ""
                for p in parts
                if p.get("type") == "text"
            ]
            prev_plain = "\n".join(plain_bits)
            out.append(
                Phase1Chunk(
                    parts=parts,
                    module=module,
                    segment=idx,
                    segment_total=total,
                    multimodal=True,
                )
            )
        else:
            body = str(unit)
            if idx > 1 and prev_plain:
                body = _bridge_prefix(prev_plain, module) + body
            prev_plain = str(unit)
            out.append(
                Phase1Chunk(
                    text=body,
                    module=module,
                    segment=idx,
                    segment_total=total,
                    multimodal=False,
                )
            )
    return out


def build_phase1_chunks_from_text(text: str, budget: int) -> list[Phase1Chunk]:
    """Chapter-first chunking for plain text documents.

    Always emits one call per chapter when multiple headings exist, so Phase1
    does not under-extract by stuffing the whole doc into a single short list.
    Chapters that exceed ``budget`` are further split.
    """
    chapters = split_text_by_headings(text)
    if not chapters:
        return []

    # Single short chapter that fits → one chunk
    if len(chapters) == 1 and estimate_text_tokens(text) <= budget:
        return [
            Phase1Chunk(
                text=chapters[0][1],
                module=chapters[0][0],
                segment=1,
                segment_total=1,
                multimodal=False,
            )
        ]

    result: list[Phase1Chunk] = []
    for module, body in chapters:
        pieces = _split_long_text(body, budget)
        result.extend(_finalize_chapter_chunks(module, pieces, multimodal=False))
    return result


def _content_parts_to_chapters(
    content_parts: list[dict[str, Any]],
) -> list[tuple[str, list[dict[str, Any]]]]:
    """Group ordered multimodal parts into chapters by heading lines."""
    chapters: list[tuple[str, list[dict[str, Any]]]] = []
    current_module = "文档开头"
    current_parts: list[dict[str, Any]] = []

    def flush() -> None:
        nonlocal current_parts
        if current_parts:
            chapters.append((current_module, current_parts))
            current_parts = []

    for part in content_parts:
        if part.get("type") != "text":
            current_parts.append(part)
            continue

        text = part.get("text") or ""
        if not text.strip():
            continue

        # File / page banner → structural split only; never use filename as module
        if is_file_separator_line(text):
            flush()
            current_module = "通用"
            current_parts.append({"type": "text", "text": text})
            continue

        lines = text.splitlines(keepends=True)
        buf: list[str] = []
        for line in lines:
            heading = detect_heading(line.rstrip("\n"))
            if heading is not None:
                if buf:
                    current_parts.append({"type": "text", "text": "".join(buf)})
                    buf = []
                flush()
                current_module = heading
                buf.append(line)
            else:
                buf.append(line)
        if buf:
            current_parts.append({"type": "text", "text": "".join(buf)})

    flush()
    if not chapters and content_parts:
        chapters = [("文档开头", list(content_parts))]
    return chapters


def build_phase1_chunks_from_parts(
    content_parts: list[dict[str, Any]],
    budget: int,
) -> list[Phase1Chunk]:
    """Chapter-first chunking for ordered multimodal parts.

    Prefer one model call per chapter so extraction density stays high even when
    the whole document fits in the context budget.
    """
    if not content_parts:
        return []

    chapters = _content_parts_to_chapters(content_parts)
    total = estimate_parts_tokens(content_parts)
    if total <= budget and len(chapters) <= 1:
        module = chapters[0][0] if chapters else "通用"
        return [
            Phase1Chunk(
                parts=list(content_parts),
                module=module,
                segment=1,
                segment_total=1,
                multimodal=True,
            )
        ]

    result: list[Phase1Chunk] = []
    for module, parts in chapters:
        packed = _pack_parts_with_image_affinity(parts, budget)
        if not packed:
            continue
        result.extend(_finalize_chapter_chunks(module, packed, multimodal=True))
    return result


# --- Back-compat helpers (used by older tests / callers) ---

def split_text_into_chunks(text: str, budget: int) -> list[str]:
    text = text or ""
    if not text.strip():
        return []
    chunks = build_phase1_chunks_from_text(text, budget)
    return [c.text for c in chunks if c.text.strip()]


def split_content_parts(
    content_parts: list[dict[str, Any]],
    budget: int,
) -> list[list[dict[str, Any]]]:
    chunks = build_phase1_chunks_from_parts(content_parts, budget)
    return [c.parts for c in chunks if c.parts]


def merge_functional_points(batches: list[list]) -> list:
    """Merge Phase-1 results; drop duplicates by (module, name)."""
    from app.gen.models import FunctionalPoint

    seen: set[tuple[str, str]] = set()
    merged: list[FunctionalPoint] = []
    for batch in batches:
        for fp in batch:
            fp.module = normalize_module_path(getattr(fp, "module", "") or "")
            key = ((fp.module or "").strip(), (fp.name or "").strip())
            if not key[1]:
                continue
            if key in seen:
                continue
            seen.add(key)
            merged.append(fp)
    for i, fp in enumerate(merged, start=1):
        fp.id = i
    return merged


__all__ = [
    "BRIDGE_CHARS",
    "CHARS_PER_TOKEN_INV",
    "IMAGE_TOKEN_COST",
    "MODULE_PATH_SEP",
    "Phase1Chunk",
    "build_phase1_chunks_from_parts",
    "build_phase1_chunks_from_text",
    "chunk_token_budget",
    "detect_heading",
    "estimate_part_tokens",
    "estimate_parts_tokens",
    "estimate_text_tokens",
    "is_file_separator_line",
    "looks_like_filename_module",
    "merge_functional_points",
    "normalize_module_path",
    "primary_module_name",
    "sanitize_module_name",
    "split_content_parts",
    "split_module_path",
    "split_text_by_headings",
    "split_text_into_chunks",
]
