"""036 US1：fetch 动作（页面 API 调用）单测。

覆盖：JS 构建（method/url/headers/body、相对 URL 语义、evidence 开关）、
value JSON 校验（非法方法/缺 url 拒绝）、结果解析（### Result 剥块 + JSON 双解码）。
"""
import json

import pytest

from core.fetch_action import (
    CHUNK_CHARS,
    build_fetch_chunk_js,
    build_fetch_info_js,
    build_fetch_js,
    build_fetch_release_js,
    parse_fetch_result,
    validate_fetch_value,
)


# ── value 校验 ──────────────────────────────────────────────────────────────


def test_validate_minimal_get():
    spec = validate_fetch_value(json.dumps({"method": "GET", "url": "/api/export"}))
    assert spec["method"] == "GET"
    assert spec["url"] == "/api/export"
    assert spec["evidence"] is False
    assert spec["headers"] == {}
    assert spec["body"] is None


def test_validate_normalizes_method_case():
    spec = validate_fetch_value(json.dumps({"method": "post", "url": "/x"}))
    assert spec["method"] == "POST"


def test_validate_full_payload():
    spec = validate_fetch_value(json.dumps({
        "method": "POST",
        "url": "https://example.com/api/import",
        "headers": {"X-Token": "t1"},
        "body": "{\"k\":1}",
        "evidence": True,
        "filename": "out.csv",
    }))
    assert spec["headers"] == {"X-Token": "t1"}
    assert spec["body"] == '{"k":1}'
    assert spec["evidence"] is True
    assert spec["filename"] == "out.csv"


def test_validate_rejects_bad_method():
    with pytest.raises(ValueError, match="method"):
        validate_fetch_value(json.dumps({"method": "HEAD", "url": "/x"}))


def test_validate_rejects_missing_url():
    with pytest.raises(ValueError, match="url"):
        validate_fetch_value(json.dumps({"method": "GET"}))


def test_validate_rejects_non_json():
    with pytest.raises(ValueError):
        validate_fetch_value("not json at all")


def test_validate_rejects_bad_headers():
    with pytest.raises(ValueError, match="headers"):
        validate_fetch_value(json.dumps({"method": "GET", "url": "/x", "headers": ["a"]}))


# ── JS 构建 ─────────────────────────────────────────────────────────────────


def test_build_fetch_js_contains_call_and_credentials():
    js = build_fetch_js(
        validate_fetch_value(json.dumps({"method": "GET", "url": "/api/export"})),
        token="abc123",
    )
    assert "fetch(" in js
    assert "credentials" in js and "include" in js
    assert "abc123" in js


def test_build_fetch_js_relative_url_kept_verbatim():
    js = build_fetch_js(
        validate_fetch_value(json.dumps({"method": "GET", "url": "/api/export?t=1"})),
        token="tok",
    )
    assert "/api/export?t=1" in js


def test_build_fetch_js_evidence_stores_body_only_when_true():
    spec_off = validate_fetch_value(json.dumps({"method": "GET", "url": "/x"}))
    js_off = build_fetch_js(spec_off, token="t1")
    assert "storeBody" not in js_off or "false" in js_off  # 不存体（由 evidence 控制）

    spec_on = validate_fetch_value(json.dumps({"method": "GET", "url": "/x", "evidence": True}))
    js_on = build_fetch_js(spec_on, token="t1")
    assert "__vt_fetch" in js_on  # 存体全局


def test_chunk_js_uses_token_and_bounds():
    info = build_fetch_info_js("tok1")
    chunk = build_fetch_chunk_js("tok1")
    release = build_fetch_release_js("tok1")
    for js in (info, chunk, release):
        assert "tok1" in js and "__vt_fetch" in js
    assert str(CHUNK_CHARS) in chunk or "size" in chunk


# ── 结果解析（### Result 剥块红线） ─────────────────────────────────────────


MCP_OK_TEXT = (
    "### Ran Playwright code\n"
    "```js\n"
    "await page.evaluate('async () => { const r = await fetch(\"/api/export\"); return JSON.stringify({ok:true,status:200,size:5}); }');\n"
    "```\n"
    "### Result\n"
    '"{\\"ok\\":true,\\"status\\":200,\\"size\\":5,\\"content_type\\":\\"text/csv\\",\\"excerpt\\":\\"a,b\\\\n\\"}"\n'
    "### Page\n"
    "- Page URL: http://127.0.0.1:8080/export\n"
)


def test_parse_result_double_encoded():
    parsed = parse_fetch_result(MCP_OK_TEXT)
    assert parsed["status"] == 200
    assert parsed["ok"] is True
    assert parsed["size"] == 5


def test_parse_result_ignores_fake_hits_outside_result_block():
    """红线：源码回显里出现 {ok:true} 不得被当结果（必须只解析 ### Result 块）。"""
    text = (
        "### Ran Playwright code\n"
        "```js\n"
        "return JSON.stringify({ok:true,status:999,size:1});\n"
        "```\n"
        "### Result\n"
        '"{\\"ok\\":false,\\"status\\":404,\\"size\\":0,\\"excerpt\\":\\"\\"}"\n'
        "### Page\n"
    )
    parsed = parse_fetch_result(text)
    assert parsed["status"] == 404


def test_parse_result_plain_json_single_encode():
    parsed = parse_fetch_result('### Result\n{"ok":true,"status":200,"size":7}\n### Page\n')
    assert parsed["status"] == 200


def test_parse_result_raises_on_missing_block():
    with pytest.raises(ValueError):
        parse_fetch_result("no result block here")


def test_parse_result_raises_on_garbage_block():
    with pytest.raises(ValueError):
        parse_fetch_result("### Result\nnot-a-json\n### Page\n")


# ── run_fetch_action 编排（分块回传/判定/release） ───────────────────────────


class FakeCallTool:
    """按调用顺序返回脚本化 evaluate 结果；记录每次调用的 function 串。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.functions = []

    async def __call__(self, tool, args):
        assert tool == "browser_evaluate"
        self.functions.append(args.get("function") or "")
        if not self.responses:
            return {"success": True, "text": "### Result\n{}\n### Page\n"}
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _mk(payload, *, ok=True):
    if not ok:
        return {"success": False, "error": str(payload)}
    return {"success": True, "text": "### Ran Playwright code\n```js\nx\n```\n### Result\n" + json.dumps(json.dumps(payload)) + "\n### Page\n"}


@pytest.mark.asyncio
async def test_run_fetch_success_simple():
    from core.fetch_action import run_fetch_action

    fake = FakeCallTool([_mk({"ok": True, "status": 200, "size": 5, "content_type": "text/csv", "excerpt": "a,b\n"})])
    out = await run_fetch_action(fake, json.dumps({"method": "GET", "url": "/api/export"}))
    assert out["success"] is True
    assert out["api"]["status"] == 200
    assert out["api"]["excerpt"].startswith("a,b")
    assert "evidence" not in out


@pytest.mark.asyncio
async def test_run_fetch_4xx_is_failure():
    from core.fetch_action import run_fetch_action

    fake = FakeCallTool([_mk({"ok": False, "status": 404, "size": 0, "excerpt": "not found"})])
    out = await run_fetch_action(fake, json.dumps({"method": "GET", "url": "/missing"}))
    assert out["success"] is False
    assert "404" in out["error"]


@pytest.mark.asyncio
async def test_run_fetch_network_error_readable():
    from core.fetch_action import run_fetch_action

    fake = FakeCallTool([_mk({"ok": False, "status": None, "size": 0, "error": "Failed to fetch"})])
    out = await run_fetch_action(fake, json.dumps({"method": "GET", "url": "https://blocked/x"}))
    assert out["success"] is False
    assert "Failed to fetch" in out["error"]


@pytest.mark.asyncio
async def test_run_fetch_evidence_chunked_and_released():
    import base64 as _b64

    from core.fetch_action import run_fetch_action

    raw = b"hello-world"
    b64 = _b64.b64encode(raw).decode()
    fake = FakeCallTool([
        _mk({"ok": True, "status": 200, "size": len(raw), "content_type": "text/plain",
             "excerpt": "hello", "stored": len(b64)}),
        _mk({"size": len(b64)}),                                   # info
        _mk({"data": b64[:4], "eof": False, "size": len(b64)}),    # chunk 1
        _mk({"data": b64[4:], "eof": True, "size": len(b64)}),     # chunk 2
        _mk("released"),                                            # release
    ])
    out = await run_fetch_action(
        fake, json.dumps({"method": "GET", "url": "/x", "evidence": True})
    )
    assert out["success"] is True
    ev = out["evidence"]
    assert ev["data"] == raw
    assert ev["truncated"] is False
    assert any("delete window" in f for f in fake.functions)  # release 已调用


@pytest.mark.asyncio
async def test_run_fetch_evidence_truncates_to_max_bytes():
    import base64 as _b64

    from core.fetch_action import run_fetch_action

    raw = b"x" * 100
    b64 = _b64.b64encode(raw).decode()
    fake = FakeCallTool([
        _mk({"ok": True, "status": 200, "size": len(raw), "stored": len(b64)}),
        _mk({"size": len(b64)}),
        _mk({"data": b64, "eof": True, "size": len(b64)}),
        _mk("released"),
    ])
    out = await run_fetch_action(
        fake, json.dumps({"method": "GET", "url": "/x", "evidence": True}), max_bytes=10
    )
    ev = out["evidence"]
    assert ev["size"] == 10
    assert ev["truncated"] is True
    assert ev["data"] == b"x" * 10


@pytest.mark.asyncio
async def test_run_fetch_release_on_failure_with_store():
    from core.fetch_action import run_fetch_action

    fake = FakeCallTool([
        _mk({"ok": False, "status": 500, "size": 3, "stored": 4}),
        _mk({"size": 4}),
        _mk({"data": "YWJj", "eof": True, "size": 4}),
        _mk("released"),
    ])
    out = await run_fetch_action(
        fake, json.dumps({"method": "GET", "url": "/x", "evidence": True})
    )
    assert out["success"] is False
    assert "500" in out["error"]
    assert out["evidence"]["data"] == b"abc"


# ── 接线断言（mcp_args / 覆盖语义 / 证据落盘） ──────────────────────────────


def test_mcp_args_registers_fetch():
    from core.mcp_args import ACTION_DESCRIPTIONS, SIDE_EFFECT_ACTIONS, TOOL_MAP

    assert TOOL_MAP.get("fetch") == "browser_evaluate"
    assert "fetch" in ACTION_DESCRIPTIONS
    assert "fetch" in SIDE_EFFECT_ACTIONS  # 传输级超时按 uncertain 不盲重试


def test_fetch_covers_interaction_step_not_assert_step():
    from core.goal_agent_loop import journal_entry_covers_checklist

    entry = {"success": True, "action": "fetch"}
    assert journal_entry_covers_checklist(entry, step_description="导出报表") is True
    assert journal_entry_covers_checklist(entry, step_description="点击【导出】按钮") is True
    assert journal_entry_covers_checklist(entry, step_description="断言页面包含【导出成功】") is False
    assert journal_entry_covers_checklist(entry, step_description="等待【加载中】消失") is False
    assert journal_entry_covers_checklist({"success": False, "action": "fetch"}, step_description="导出报表") is False


def test_evidence_filename_prefers_model_name_and_ct_ext():
    from core.fetch_action import evidence_filename

    assert evidence_filename("out.csv", "text/csv") == "out.csv"
    assert evidence_filename("报表", "text/csv") == "报表.csv"
    assert evidence_filename(None, "application/json").endswith(".json")
    assert evidence_filename(None, "application/octet-stream").endswith(".bin")
    assert "/" not in evidence_filename("../../etc/passwd", "text/plain")


def test_save_fetch_evidence_file_writes_and_reports(tmp_path, monkeypatch):
    from core.fetch_action import save_fetch_evidence_file

    monkeypatch.chdir(tmp_path)
    ev = {"data": b"a,b\n1,2\n", "size": 8, "truncated": False}
    out = save_fetch_evidence_file("reports/agent_x/evidence", ev, "report.csv", "text/csv")
    assert out["evidence_path"] == "reports/agent_x/evidence/report.csv"
    assert out["evidence_size"] == 8
    assert out["evidence_truncated"] is False
    written = tmp_path / "reports/agent_x/evidence/report.csv"
    assert written.read_bytes() == b"a,b\n1,2\n"
