"""Unit tests for dropdown NL step normalization."""
from core.step_executor import (
    normalize_step_description,
    option_choice_visible_in_snapshot,
    parse_dropdown_select,
)


def test_parse_dropdown_plain():
    assert parse_dropdown_select("单位下拉框选择【汉东省院】") == ("单位", "汉东省院")


def test_parse_dropdown_bracket_label():
    assert parse_dropdown_select("在【单位】下拉中选择【汉东省院】") == ("单位", "汉东省院")


def test_parse_dropdown_click_then_click():
    assert parse_dropdown_select("点击单位下拉框，点击【汉东省院】单位") == ("单位", "汉东省院")


def test_parse_dropdown_click_bracket_then_select():
    assert parse_dropdown_select("点击【单位】下拉框后选择【汉东省院】") == ("单位", "汉东省院")


def test_parse_dropdown_in_field_select():
    assert parse_dropdown_select("在【单位】中选择【汉东省院】") == ("单位", "汉东省院")


def test_parse_dropdown_not_plain_two_buttons():
    assert parse_dropdown_select("点击【登录】，点击【提交】") == (None, None)


def test_normalize_avoids_literal_dropdown_word():
    out = normalize_step_description("单位下拉框选择【汉东省院】")
    assert "汉东省院" in out
    assert "单位" in out
    assert "不要查找" in out
    assert "单位下拉框" in out  # warned against in instruction


def test_option_choice_visible_roles():
    snap = '- combobox "单位" [ref=e1]\n- option "汉东省院" [ref=e2]'
    assert option_choice_visible_in_snapshot(snap, "汉东省院") is True
    assert option_choice_visible_in_snapshot(snap, "北京市院") is False
    # Plain text mention without option role should NOT count as open list
    snap2 = '- combobox "单位" [ref=e1]\n- text: 汉东省院已存在于表格'
    assert option_choice_visible_in_snapshot(snap2, "汉东省院") is False
