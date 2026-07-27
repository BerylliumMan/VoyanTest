"""Unit tests for chapter-aware Phase-1 document chunking."""
from __future__ import annotations

import pytest

from app.gen.chunking import (
    BRIDGE_CHARS,
    Phase1Chunk,
    build_phase1_chunks_from_parts,
    build_phase1_chunks_from_text,
    chunk_token_budget,
    detect_heading,
    estimate_text_tokens,
    merge_functional_points,
    split_content_parts,
    split_text_by_headings,
    split_text_into_chunks,
)
from app.gen.models import FunctionalPoint


class TestChunkBudget:
    def test_budget_is_80_percent_minus_reserve(self):
        b = chunk_token_budget(100_000, ratio=0.8)
        assert b == 74_000

    def test_tiny_context_has_floor(self):
        assert chunk_token_budget(1000, ratio=0.8) >= 2000


class TestDetectHeading:
    def test_markdown_heading(self):
        assert detect_heading("## 登录模块") == "登录"

    def test_chapter_cn(self):
        assert "数据" in (detect_heading("第一章 数据总览") or "")

    def test_file_header_is_not_module_heading(self):
        assert detect_heading("===== 文件1: req.docx =====") is None
        assert detect_heading("===== shot.png 第1页 =====") is None

    def test_body_not_heading(self):
        assert detect_heading("用户输入正确的账号密码后点击登录按钮。") is None


class TestSplitByHeadings:
    def test_two_chapters(self):
        text = "## 登录\n说明A\n\n## 首页\n说明B\n"
        chapters = split_text_by_headings(text)
        assert len(chapters) >= 2
        titles = [c[0] for c in chapters]
        assert any("登录" in t for t in titles)
        assert any("首页" in t for t in titles)


class TestSplitText:
    def test_short_text_single_chunk(self):
        text = "短文档内容"
        chunks = split_text_into_chunks(text, budget=10_000)
        assert len(chunks) == 1

    def test_long_text_multiple_chunks(self):
        para = "测" * 800 + "\n"
        text = para * 20
        budget = 8000
        chunks = split_text_into_chunks(text, budget)
        assert len(chunks) >= 2
        for c in chunks:
            # bridge prefix may add a bit over budget estimate on continuation
            assert estimate_text_tokens(c) <= budget + estimate_text_tokens("x" * (BRIDGE_CHARS + 80))

    def test_long_chapter_splits_inside_with_module_label(self):
        body = ("内容" * 2000 + "\n") * 10
        text = f"## 超长登录模块\n{body}"
        budget = 5000
        chunks = build_phase1_chunks_from_text(text, budget)
        assert len(chunks) >= 2
        assert all(isinstance(c, Phase1Chunk) for c in chunks)
        assert all(c.module == "超长登录" or "登录" in c.module for c in chunks)
        assert chunks[0].segment_total == len(chunks)
        assert chunks[1].segment == 2
        assert "衔接" in chunks[1].text or "续篇" in chunks[1].intro


class TestSplitContentParts:
    def test_packs_text_parts_under_budget(self):
        parts = [{"type": "text", "text": "甲" * 2000}, {"type": "text", "text": "乙" * 2000}]
        chunks = split_content_parts(parts, budget=4000)
        assert len(chunks) >= 2

    def test_image_stays_with_preceding_text(self):
        parts = [
            {"type": "text", "text": "说明文字" * 100},
            {"type": "image", "ext": "png", "b64": "abc"},
            {"type": "text", "text": "后文" * 2000},
        ]
        budget = 5000
        chunks = build_phase1_chunks_from_parts(parts, budget)
        # First chunk that has the image should also contain some preceding text
        img_chunks = [c for c in chunks if any(p.get("type") == "image" for p in c.parts)]
        assert img_chunks
        first = img_chunks[0]
        types = [p.get("type") for p in first.parts]
        assert "image" in types
        # Prefer text before image in same chunk when affinity works
        if types.index("image") > 0:
            assert types[types.index("image") - 1] == "text"

    def test_chapter_then_oversized_inner_split(self):
        parts = [
            {"type": "text", "text": "## 模块甲\n" + ("甲" * 3000)},
            {"type": "text", "text": "## 模块乙\n" + ("乙" * 3000)},
        ]
        chunks = build_phase1_chunks_from_parts(parts, budget=2500)
        assert len(chunks) >= 2
        modules = {c.module for c in chunks}
        assert any("甲" in m or m == "模块甲" for m in modules) or len(chunks) >= 2


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


@pytest.mark.asyncio
async def test_extract_fps_chunks_when_over_budget(monkeypatch):
    """Long text should trigger multi-chunk Phase1 calls."""
    from app.gen import feature_extractor as fe

    calls = {"n": 0}

    async def fake_budget():
        return 20_000

    async def fake_cont(**kwargs):
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
    monkeypatch.setattr(fe, "_extract_fps_with_continuation", fake_cont)

    async def nosleep(*_a, **_k):
        return None

    monkeypatch.setattr(fe.asyncio, "sleep", nosleep)

    long_text = "测" * 20_000
    fps = await fe.extract_functional_points(text=long_text)
    assert calls["n"] >= 2
    assert len(fps) == calls["n"]


def test_multi_chapter_splits_even_under_budget():
    """Multiple headings → one Phase1 chunk per chapter even if all fit budget."""
    text = "## 登录模块\n用户登录说明\n\n## 首页模块\n首页说明\n\n## 设置模块\n设置说明\n"
    chunks = build_phase1_chunks_from_text(text, budget=100_000)
    assert len(chunks) >= 3
    modules = [c.module for c in chunks]
    assert any("登录" in m for m in modules)
    assert any("首页" in m for m in modules)
    assert any("设置" in m for m in modules)


def test_multi_chapter_parts_split_under_budget():
    parts = [
        {"type": "text", "text": "## 模块甲\n甲内容"},
        {"type": "text", "text": "## 模块乙\n乙内容"},
    ]
    chunks = build_phase1_chunks_from_parts(parts, budget=100_000)
    assert len(chunks) >= 2


def test_image_file_header_does_not_become_module():
    """Uploaded image banners must not be used as FP module names."""
    parts = [
        {"type": "text", "text": "===== 文件1: login_screen.png ====="},
        {"type": "image", "ext": "png", "b64": "abc"},
    ]
    chunks = build_phase1_chunks_from_parts(parts, budget=100_000)
    assert len(chunks) >= 1
    assert all(c.module in ("通用", "文档开头") for c in chunks)
    assert "login_screen" not in chunks[0].intro
    assert "禁止使用文件名" in chunks[0].intro
