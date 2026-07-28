"""Unit tests for dropdown NL step normalization."""
from core.step_executor import normalize_step_description, parse_dropdown_select


def test_parse_dropdown_plain():
    assert parse_dropdown_select("单位下拉框选择【汉东省院】") == ("单位", "汉东省院")


def test_parse_dropdown_bracket_label():
    assert parse_dropdown_select("在【单位】下拉中选择【汉东省院】") == ("单位", "汉东省院")


def test_normalize_avoids_literal_dropdown_word():
    out = normalize_step_description("单位下拉框选择【汉东省院】")
    assert "汉东省院" in out
    assert "单位" in out
    assert "不要查找" in out
    assert "单位下拉框" in out  # warned against in instruction
