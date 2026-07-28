"""Unit tests for gen upload progress stage mapping (monotonic)."""
from app.routers.gen.upload import map_gen_progress


def test_phase1_maps_into_15_55():
    assert map_gen_progress(0, 4, "正在提取测试项") == 15
    assert map_gen_progress(2, 4, "正在提取测试项") == 35
    assert map_gen_progress(4, 4, "正在提取测试项") == 55


def test_phase2_maps_into_55_95():
    assert map_gen_progress(0, 2, "正在生成用例 Batch 1") == 55
    assert map_gen_progress(1, 2, "正在生成用例") == 75
    assert map_gen_progress(2, 2, "生成完成") == 95


def test_floor_prevents_regression():
    assert map_gen_progress(0, 4, "正在提取测试项", floor=40) == 40
    assert map_gen_progress(0, 10, "解析文档", floor=50) == 50


def test_validate_near_end():
    assert map_gen_progress(1, 1, "正在校验") >= 96
