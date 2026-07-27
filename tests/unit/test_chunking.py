"""Unit tests for Phase-1 document chunking at ~80% context budget."""
from __future__ import annotations

import pytest

from app.gen.chunking import (
    chunk_token_budget,
    estimate_text_tokens,
    merge_functional_points,
    split_content_parts,
    split_text_into_chunks,
)
from app.gen.models import FunctionalPoint


class TestChunkBudget:
    def test_budget_is_80_percent_minus_reserve(self):
        b = chunk_token_budget(100_000, ratio=0.8)
        # 80000 - 6000 reserved
        assert b == 74_000

    def test_tiny_context_has_floor(self):
        assert chunk_token_budget(1000, ratio=0.8) >= 2000


class TestSplitText:
    def test_short_text_single_chunk(self):
        text = "短文档内容"
        chunks = split_text_into_chunks(text, budget=10_000)
        assert chunks == [text]

    def test_long_text_multiple_chunks(self):
        # ~1500 tokens per 1000 chars at 1.5x → need many chars for 2+ chunks
        para = "测" * 800 + "\n"
        text = para * 20  # ~24000 chars ≈ 36000 tokens
        budget = 8000
        chunks = split_text_into_chunks(text, budget)
        assert len(chunks) >= 2
        for c in chunks:
            assert estimate_text_tokens(c) <= budget + 50  # small slack for newlines


class TestSplitContentParts:
    def test_packs_text_parts_under_budget(self):
        parts = [{"type": "text", "text": "甲" * 2000}, {"type": "text", "text": "乙" * 2000}]
        # each ~3000 tokens
        chunks = split_content_parts(parts, budget=4000)
        assert len(chunks) >= 2

    def test_image_alone_when_needed(self):
        parts = [
            {"type": "text", "text": "前" * 3000},
            {"type": "image", "ext": "png", "b64": "abc"},
        ]
        chunks = split_content_parts(parts, budget=3500)
        assert any(any(p.get("type") == "image" for p in ch) for ch in chunks)


class TestMergeFps:
    def test_dedupe_by_module_name(self):
        a = [
            FunctionalPoint(id=1, module="登录", name="成功登录", description="d1"),
            FunctionalPoint(id=2, module="登录", name="密码错误", description="d2"),
        ]
        b = [
            FunctionalPoint(id=1, module="登录", name="成功登录", description="dup"),
            FunctionalPoint(id=3, module="首页", name="打开首页", description="d3"),
        ]
        merged = merge_functional_points([a, b])
        assert len(merged) == 3
        assert [fp.id for fp in merged] == [1, 2, 3]
        names = {(fp.module, fp.name) for fp in merged}
        assert ("登录", "成功登录") in names
        assert ("首页", "打开首页") in names


@pytest.mark.asyncio
async def test_extract_fps_chunks_when_over_budget(monkeypatch):
    """Long text should trigger multi-chunk Phase1 calls."""
    from app.gen import feature_extractor as fe

    calls = {"n": 0}

    async def fake_budget():
        return 20_000  # chunk budget ≈ 10000 after reserve

    async def fake_once(**kwargs):
        calls["n"] += 1
        return [
            FunctionalPoint(
                id=calls["n"],
                module="M",
                name=f"项{calls['n']}",
                description="d",
            )
        ]

    monkeypatch.setattr(fe, "get_context_budget", fake_budget)
    monkeypatch.setattr(fe, "_extract_fps_once", fake_once)

    async def nosleep(*_a, **_k):
        return None

    monkeypatch.setattr(fe.asyncio, "sleep", nosleep)

    long_text = "测" * 20_000  # ~30000 tokens > budget
    fps = await fe.extract_functional_points(text=long_text)
    assert calls["n"] >= 2
    assert len(fps) == calls["n"]
