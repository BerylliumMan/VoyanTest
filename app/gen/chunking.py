"""Split document content so each Phase-1 model call stays within a token budget.

Budget defaults to ``max_context_tokens * 0.8`` (minus reserved prompt/output
headroom). Text is estimated at ~1.5 tokens/char (Chinese-heavy docs); each
image part is charged a fixed vision cost.
"""
from __future__ import annotations

import logging
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
            # Character window for a single oversized paragraph
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


def split_text_into_chunks(text: str, budget: int) -> list[str]:
    text = text or ""
    if not text.strip():
        return []
    return _split_long_text(text, budget)


def split_content_parts(
    content_parts: list[dict[str, Any]],
    budget: int,
) -> list[list[dict[str, Any]]]:
    """Pack ordered multimodal parts into chunks under ``budget`` tokens.

    Oversized text parts are split; a single image that exceeds the budget is
    still emitted alone (cannot shrink further without dropping it).
    """
    if not content_parts:
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

    for part in content_parts:
        ptype = part.get("type")
        if ptype == "text":
            text = part.get("text") or ""
            if not text.strip():
                continue
            pieces = _split_long_text(text, budget)
            for piece in pieces:
                pt = estimate_text_tokens(piece)
                if current and current_tokens + pt > budget:
                    flush()
                current.append({"type": "text", "text": piece})
                current_tokens += pt
                if current_tokens >= budget:
                    flush()
            continue

        # image or other
        pt = estimate_part_tokens(part)
        if current and current_tokens + pt > budget:
            flush()
        current.append(part)
        current_tokens += pt
        if current_tokens >= budget:
            flush()

    flush()
    return chunks


def merge_functional_points(batches: list[list]) -> list:
    """Merge Phase-1 results; drop duplicates by (module, name)."""
    from app.gen.models import FunctionalPoint

    seen: set[tuple[str, str]] = set()
    merged: list[FunctionalPoint] = []
    for batch in batches:
        for fp in batch:
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
    "CHARS_PER_TOKEN_INV",
    "IMAGE_TOKEN_COST",
    "chunk_token_budget",
    "estimate_part_tokens",
    "estimate_parts_tokens",
    "estimate_text_tokens",
    "merge_functional_points",
    "split_content_parts",
    "split_text_into_chunks",
]
