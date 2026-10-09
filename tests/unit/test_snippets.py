"""US11 片段生成器（cURL/Python/JS）+ 打码 —— 033-api-test-maturity。

TDD：先于实现编写（预期红：core.snippets 不存在）。
"""

from __future__ import annotations

from core.snippets import build_snippets, mask_snippet_inputs
from app.parsers.api_doc.curl import parse_curl


def _sample():
    return {
        "method": "POST",
        "url": "https://api.example.com/orders?page=2",
        "headers": {"Content-Type": "application/json", "X-Token": "t1"},
        "body_text": '{"a": 1}',
    }


def test_curl_snippet_format():
    snip = build_snippets(**_sample())["curl"]
    assert snip.startswith("curl -X POST 'https://api.example.com/orders?page=2'")
    assert "-H 'Content-Type: application/json'" in snip
    assert "--data-raw '{\"a\": 1}'" in snip


def test_python_snippet_format():
    snip = build_snippets(**_sample())["python"]
    assert "import requests" in snip
    assert "requests.request('POST'" in snip
    assert "https://api.example.com/orders?page=2" in snip
    assert "X-Token" in snip


def test_js_snippet_format():
    snip = build_snippets(**_sample())["js"]
    assert "fetch(" in snip and '"POST"' in snip
    assert "X-Token" in snip


def test_multipart_hint_skips_content_type():
    snip = build_snippets(
        method="POST",
        url="https://x/up",
        headers={"Content-Type": "multipart/form-data; boundary=x"},
        body_text="",
    )["curl"]
    assert "Content-Type" not in snip
    assert "-F" in snip


def test_mask_inputs_hides_secret_values_and_headers():
    url, headers, body = mask_snippet_inputs(
        url="https://x/a?k=sup3r",
        headers={"Authorization": "Bearer sup3r", "X-K": "1"},
        body_text='{"pwd": "sup3r"}',
        secret_values=["sup3r"],
    )
    assert "sup3r" not in url and "sup3r" not in headers.get("Authorization", "")
    assert "sup3r" not in body
    assert headers["X-K"] == "1"


def test_round_trip_curl_parse_consistency():
    """生成的 curl 片段应能被解析器还原（方法/URL/头/体一致）。"""
    src = _sample()
    snip = build_snippets(**src)["curl"]
    step = parse_curl(snip)
    req = step["request"]
    assert req["method"] == src["method"]
    assert req["url"] == src["url"]
    header_map = {h["key"]: h["value"] for h in req["headers"]}
    assert header_map.get("Content-Type") == "application/json"
    assert header_map.get("X-Token") == "t1"
    assert req["body"]["content"] == '{"a": 1}'


def test_empty_body_no_data_flag():
    snip = build_snippets(method="GET", url="https://x/a", headers={}, body_text="")["curl"]
    assert "--data-raw" not in snip
