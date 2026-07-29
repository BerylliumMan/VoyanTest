"""Unit tests for locator memory fingerprint extract / match / safety."""

from core.locator_memory import (
    extract_from_snapshot,
    fingerprint_matches_step_description,
    format_hint_for_agent,
    parse_snapshot_elements,
    resolve_ref,
    url_hint_ok,
)

SAMPLE = """
Page URL: https://example.com/login
Page Title: Login
- heading "登录" [ref=e1]
- textbox "用户名" [ref=e10]
- textbox "密码" [ref=e11]
- button "登录" [ref=e15]
- button "取消" [ref=e16]
"""

AMBIGUOUS = """
Page URL: https://example.com/a
- button "登录" [ref=e1]
- button "登录" [ref=e2]
"""


def test_parse_snapshot_elements():
    rows = parse_snapshot_elements(SAMPLE)
    assert any(r["ref"] == "e15" and r["role"] == "button" and r["name"] == "登录" for r in rows)


def test_extract_from_snapshot():
    fp = extract_from_snapshot(SAMPLE, "e15", action="click")
    assert fp is not None
    assert fp["role"] == "button"
    assert fp["name"] == "登录"
    assert fp["page_url_hint"] == "/login"
    assert fp["action"] == "click"


def test_resolve_unique():
    fp = {"role": "button", "name": "登录", "page_url_hint": "/login"}
    assert resolve_ref(SAMPLE, fp, step_description="点击【登录】") == "e15"


def test_resolve_ambiguous():
    fp = {"role": "button", "name": "登录", "page_url_hint": ""}
    assert resolve_ref(AMBIGUOUS, fp, step_description="点击【登录】") is None


def test_url_hint_blocks():
    fp = {"role": "button", "name": "登录", "page_url_hint": "/other"}
    assert url_hint_ok(fp, SAMPLE) is False
    assert resolve_ref(SAMPLE, fp, step_description="点击【登录】") is None


def test_step_description_gate():
    fp = {"name": "登录"}
    assert fingerprint_matches_step_description(fp, "点击【登录】按钮")
    assert not fingerprint_matches_step_description(fp, "点击【提交】")


def test_format_hint():
    hint = format_hint_for_agent({"action": "click", "role": "button", "name": "登录"})
    assert "登录" in hint
    assert "button" in hint
