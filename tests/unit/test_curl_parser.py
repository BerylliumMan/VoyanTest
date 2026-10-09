"""US11 cURL 解析器（粘贴导入）——033-api-test-maturity。

TDD：先于实现编写（预期红：app.parsers.api_doc.curl 不存在）。
"""

from __future__ import annotations

import pytest

from app.parsers.api_doc.curl import CurlParseError, parse_curl


def _req(step):
    return step["request"]


def test_simple_get_with_header():
    step = parse_curl("curl -H 'Accept: application/json' https://api.example.com/items")
    req = _req(step)
    assert req["method"] == "GET"
    assert req["url"] == "https://api.example.com/items"
    assert {"key": "Accept", "value": "application/json", "enable": True} in req["headers"]


def test_post_with_json_data_sets_json_type():
    step = parse_curl(
        'curl -X POST https://x/orders -H "Content-Type: application/json" '
        "--data-raw '{\"a\": 1}'"
    )
    req = _req(step)
    assert req["method"] == "POST"
    assert req["body"]["type"] == "json"
    assert req["body"]["content"] == '{"a": 1}'


def test_data_implies_post_and_multiple_data_joined():
    step = parse_curl("curl https://x/f -d 'a=1' -d 'b=2'")
    assert _req(step)["method"] == "POST"
    assert _req(step)["body"]["content"] == "a=1&b=2"
    assert _req(step)["body"]["type"] in ("form", "raw")


def test_form_multipart_with_file():
    step = parse_curl("curl https://x/up -F 'name=alice' -F 'file=@/tmp/a.png'")
    req = _req(step)
    assert req["body"]["type"] == "form_data"
    fields = req["body"]["form_data"]
    assert any(f["key"] == "name" and f["value"] == "alice" for f in fields)
    files = req["body"].get("files") or []
    assert any(f["key"] == "file" and f["path"] == "/tmp/a.png" for f in files)


def test_basic_auth_from_u_flag():
    step = parse_curl("curl -u 'user:pass' https://x/private")
    assert _req(step)["auth"] == {"type": "basic", "username": "user", "password": "pass"}


def test_line_continuation_and_double_quotes():
    step = parse_curl(
        "curl -X PUT \\\n"
        '  "https://x/items/1" \\\n'
        '  -H "X-Token: t1" \\\n'
        '  --data-raw "{\\"k\\": \\"v\\"}"'
    )
    req = _req(step)
    assert req["method"] == "PUT" and req["url"] == "https://x/items/1"
    assert {"key": "X-Token", "value": "t1", "enable": True} in req["headers"]
    assert req["body"]["content"] == '{"k": "v"}'


def test_json_flag():
    step = parse_curl("curl --json '{\"a\": 1}' https://x/j")
    req = _req(step)
    assert req["method"] == "POST"
    assert req["body"]["type"] == "json"


def test_ignores_common_noise_flags():
    step = parse_curl("curl -s -k -L --compressed https://x/ok")
    assert _req(step)["url"] == "https://x/ok"


def test_url_flag_and_leading_prompt():
    step = parse_curl("$ curl --url https://x/u")
    assert _req(step)["url"] == "https://x/u"


def test_rejects_missing_url():
    with pytest.raises(CurlParseError):
        parse_curl("curl -X POST -H 'A: b'")


def test_rejects_non_curl_input():
    with pytest.raises(CurlParseError):
        parse_curl("wget https://x/a")
    with pytest.raises(CurlParseError):
        parse_curl("")


def test_step_shape_is_editor_compatible():
    step = parse_curl("curl https://x/a")
    assert step["order"] == 1 and step["enable"] is True
    assert step["assertions"] == [] and step["extractors"] == []
    assert set(_req(step)) >= {"method", "url", "headers", "query", "body", "auth"}
