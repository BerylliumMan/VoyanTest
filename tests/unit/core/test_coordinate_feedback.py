"""038 US1：坐标回读反馈。"""
from __future__ import annotations

import pytest

from core.coordinate_feedback import (
    build_feedback_text,
    feedback_from_result,
    parse_coordinates,
    read_target,
)


class _Browser:
    def __init__(self, value):
        self.value = value

    async def evaluate_json(self, fn):
        return self.value


def test_parse_coordinates():
    assert parse_coordinates("120,340") == [120, 340]
    assert parse_coordinates("10,20,300,200") == [10, 20, 300, 200]
    assert parse_coordinates("bad") == []


def test_feedback_hit_and_miss():
    hit = build_feedback_text({"name": "新增", "role": "button", "selector": "#add"}, 10, 20)
    assert "button" in hit and "新增" in hit and "#add" in hit
    miss = build_feedback_text(None, 10, 20)
    assert "未命中" in miss


def test_feedback_streak_threshold():
    text = build_feedback_text(None, 1, 2, streak=3)
    assert "连续 3 次" in text


def test_feedback_read_failed():
    text = build_feedback_text(None, 1, 2, read_failed=True)
    assert "回读失败" in text


@pytest.mark.asyncio
async def test_read_target_hit_miss_and_failure():
    info, failed = await read_target(
        _Browser({"name": "登录", "role": "button", "tag": "button", "selector": "#login"}),
        5, 6,
    )
    assert failed is False and info and info["name"] == "登录"

    info, failed = await read_target(_Browser({}), 5, 6)
    assert failed is False and info is None

    info, failed = await read_target(_Browser(None), 5, 6)
    assert failed is True and info is None


def test_feedback_from_result():
    action = {"name": "click_xy", "args": {"value": "100,200"}}
    result = {"success": True, "_resolved": {"name": "新增", "role": "button"}}
    text = feedback_from_result(action, result)
    assert text and "新增" in text
    assert feedback_from_result({"name": "click_xy", "args": {"value": "1,2"}},
                                {"success": True}) is not None  # miss 也有反馈
    assert feedback_from_result({"name": "click", "args": {}}, {"success": True}) is None
    assert feedback_from_result(action, {"success": False}) is None
