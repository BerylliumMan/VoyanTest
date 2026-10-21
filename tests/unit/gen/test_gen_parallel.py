"""038 US5：生成批次有界并行。"""
from __future__ import annotations

import asyncio

import pytest

from app.gen.models import FunctionalPoint, TestCase


def _fps(n: int, offset: int = 0):
    return [
        FunctionalPoint(id=offset + i, module=f"M{(offset+i)%2}",
                        name=f"FP{offset+i}", description="d")
        for i in range(n)
    ]


@pytest.mark.asyncio
async def test_parallel_batches_bounded_and_renumbered(monkeypatch):
    import app.gen.feature_extractor as fe

    active = {"now": 0, "peak": 0}

    async def _fake_batch_once(**kwargs):
        active["now"] += 1
        active["peak"] = max(active["peak"], active["now"])
        try:
            await asyncio.sleep(0.05)
            batch = kwargs["batch"]
            out = []
            for fp in batch:
                tc = TestCase(title=f"TC {fp.name}", module=fp.module)
                tc.fp_name = fp.name
                out.append(tc)
            return out
        finally:
            active["now"] -= 1

    monkeypatch.setattr(fe, "_generate_batch_once", _fake_batch_once)
    progress = []
    out = await fe._parallel_generate_batches(
        [_fps(2, 0), _fps(2, 2), _fps(2, 4)],
        tc_prompt="p", project_description="", agent_type="generation", agent_id=None,
        per_item=1, content_parts=None, page_inventory=None,
        progress_callback=lambda step, total, msg: progress.append((step, msg)),
        phase1_offset=1, total_steps=4, cancel_checker=None, parallel=2,
    )
    # 并发不超过上限
    assert active["peak"] == 2
    # 顺序保持、编号连续
    assert [tc.title for tc in out["test_cases"]] == [
        "TC FP0", "TC FP1", "TC FP2", "TC FP3", "TC FP4", "TC FP5",
    ]
    assert [tc.test_case_id for tc in out["test_cases"]] == [f"TC-{i:03d}" for i in range(1, 7)]
    assert len(progress) == 3


@pytest.mark.asyncio
async def test_parallel_passes_skip_pace(monkeypatch):
    import app.gen.feature_extractor as fe

    seen = []

    async def _fake_batch_once(**kwargs):
        seen.append(kwargs.get("skip_pace"))
        tc = TestCase(module="M")
        tc.fp_name = kwargs["batch"][0].name
        return [tc]

    monkeypatch.setattr(fe, "_generate_batch_once", _fake_batch_once)
    out = await fe._parallel_generate_batches(
        [_fps(1, 0), _fps(1, 1)],
        tc_prompt="p", project_description="", agent_type="generation", agent_id=None,
        per_item=1, content_parts=None, page_inventory=None,
        progress_callback=None, phase1_offset=1, total_steps=3,
        cancel_checker=None, parallel=2,
    )
    assert seen == [True, True]
    assert len(out["test_cases"]) == 2


def test_batch_hint_contains_rules():
    import app.gen.feature_extractor as fe

    hint = fe._build_batch_user_hint(_fps(2), per_item=2, flow_mode=False, has_images=False)
    assert "禁止" in hint and "fp_name" in hint
    flow = fe._build_batch_user_hint(_fps(2), per_item=2, flow_mode=True, has_images=False)
    assert "文档流程" in flow
