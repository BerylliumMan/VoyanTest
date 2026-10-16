"""036 US3：诊断脚本 SDK（http/db/platform/files）强制矩阵单测。

覆盖（contracts C4）：
- db：写语句/多语句/注释隐藏写/敏感表拒绝；SELECT（含前导注释）放行；行上限 + 截断标注
- http：单次超时钳制 ≤10s；响应 ≤2MB 截断标注；JSON 解析
- files：data/、reports/ 白名单 + 越界拒绝；>1MB 截断；二进制拒绝
- platform：包装 tool_queries（身份绑定 user_id 强制）
- 计数：http_calls / db_queries 入审计计数
"""
from __future__ import annotations

import asyncio

import pytest


def _run_sync(coro):
    """测试用同步桥：新事件循环执行协程（生产用 run_coroutine_threadsafe）。"""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _FakeResult:
    def __init__(self, rows, columns):
        self._rows = rows
        self._columns = columns

    def keys(self):
        return list(self._columns)

    def fetchmany(self, n):
        return self._rows[:n]


class _FakeSession:
    def __init__(self, rows=None, columns=("a",), capture=None):
        self._rows = rows if rows is not None else [(1,)]
        self._columns = columns
        self._capture = capture if capture is not None else {}

    async def execute(self, stmt, params=None):
        self._capture["sql"] = str(stmt)
        self._capture["params"] = params
        return _FakeResult(self._rows, self._columns)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _make_sdk(rows=None, columns=("a",), capture=None, user_id=7):
    from app.services.script_sdk import SdkState, build_sdk

    state = SdkState()
    fake = _FakeSession(rows=rows, columns=columns, capture=capture)
    sdk = build_sdk(
        run_async=_run_sync,
        db_factory=lambda: fake,
        user_id=user_id,
        state=state,
    )
    return sdk, state, fake


# ── db 命名空间 ────────────────────────────────────────────────────────────


def test_db_rejects_write_statements():
    sdk, _, _ = _make_sdk()
    for sql in (
        "INSERT INTO test_cases (name) VALUES ('x')",
        "UPDATE test_cases SET name='x'",
        "DELETE FROM test_cases",
        "DROP TABLE test_cases",
        "TRUNCATE test_cases",
        "SELECT * FROM test_cases; DROP TABLE test_cases",
        "SELECT 1; DELETE FROM test_cases",
    ):
        with pytest.raises(ValueError):
            sdk["db"].query(sql)


def test_db_rejects_comment_hidden_write():
    sdk, _, _ = _make_sdk()
    with pytest.raises(ValueError):
        sdk["db"].query("/* SELECT */ DELETE FROM test_cases")
    with pytest.raises(ValueError):
        sdk["db"].query("-- harmless\nUPDATE test_cases SET name='x'")


def test_db_rejects_sensitive_tables():
    sdk, _, _ = _make_sdk()
    for sql in (
        "SELECT * FROM users",
        "SELECT id, password_hash FROM users WHERE id=1",
        "SELECT token_hash FROM api_tokens",
        "SELECT * FROM audit_logs",
        "SELECT * FROM sessions",
        "WITH x AS (SELECT * FROM users) SELECT * FROM x",
    ):
        with pytest.raises(ValueError):
            sdk["db"].query(sql)


def test_db_allows_select_with_leading_comment():
    capture = {}
    sdk, state, _ = _make_sdk(rows=[(1,), (2,)], columns=("id",), capture=capture)
    out = sdk["db"].query("/* 取证 */ SELECT id FROM test_cases")
    assert out["columns"] == ["id"]
    assert out["rows"] == [[1], [2]]
    assert out["truncated"] is False
    assert state.db_queries == 1
    # 参数透传
    sdk["db"].query("SELECT id FROM test_cases WHERE id = :pid", {"pid": 3})
    assert capture["params"] == {"pid": 3}


def test_db_row_cap_and_truncation():
    rows = [(i,) for i in range(300)]
    sdk, _, _ = _make_sdk(rows=rows, columns=("id",))
    out = sdk["db"].query("SELECT id FROM test_cases")
    assert len(out["rows"]) == 200
    assert out["truncated"] is True


def test_db_empty_and_non_select_rejected():
    sdk, _, _ = _make_sdk()
    with pytest.raises(ValueError):
        sdk["db"].query("")
    with pytest.raises(ValueError):
        sdk["db"].query("EXPLAIN ANALYZE DELETE FROM x")  # 写关键字仍拒绝
    # WITH ... SELECT 允许
    out = sdk["db"].query("WITH t AS (SELECT 1 AS a) SELECT a FROM t")
    assert out["columns"] == ["a"]


# ── http 命名空间 ─────────────────────────────────────────────────────────


class _FakeResponse:
    def __init__(self, status=200, content=b"hello", headers=None, json_data=None):
        self.status_code = status
        self.content = content
        self.headers = headers or {"content-type": "text/plain"}
        self._json = json_data

    def json(self):
        if self._json is None:
            raise ValueError("not json")
        return self._json


class _FakeClient:
    calls = []

    def __init__(self, *a, **kw):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def request(self, method, url, **kw):
        _FakeClient.calls.append({"method": method, "url": url, "kw": kw})
        return _FakeResponse(status=200, content=b"ok", json_data={"k": 1})


def test_http_timeout_clamped_and_json():
    from app.services.script_sdk import SdkState, build_sdk

    state = SdkState()
    _FakeClient.calls = []
    sdk = build_sdk(
        run_async=_run_sync,
        db_factory=lambda: _FakeSession(),
        user_id=1,
        state=state,
        http_client_factory=_FakeClient,
    )
    out = sdk["http"].get("http://example.com/api", timeout=999)
    assert out["status"] == 200
    assert out["json"] == {"k": 1}
    assert _FakeClient.calls[0]["kw"]["timeout"] == 10  # 钳制 ≤10s
    assert state.http_calls == 1

    sdk["http"].request("POST", "http://example.com/x", json={"a": 1}, timeout=0.5)
    assert _FakeClient.calls[1]["method"] == "POST"
    assert _FakeClient.calls[1]["kw"]["timeout"] == 0.5
    assert state.http_calls == 2


def test_http_response_truncated_at_2mb():
    from app.services.script_sdk import SdkState, build_sdk

    class _BigClient(_FakeClient):
        def request(self, method, url, **kw):
            return _FakeResponse(status=200, content=b"x" * (2 * 1024 * 1024 + 10))

    sdk = build_sdk(
        run_async=_run_sync,
        db_factory=lambda: _FakeSession(),
        user_id=1,
        state=SdkState(),
        http_client_factory=_BigClient,
    )
    out = sdk["http"].get("http://example.com/big")
    assert out["truncated"] is True
    assert len(out["text"].encode()) == 2 * 1024 * 1024


# ── files 命名空间 ────────────────────────────────────────────────────────


def test_files_whitelist_and_traversal(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    (tmp_path / "reports").mkdir()
    (tmp_path / "data" / "a.txt").write_text("hello-data", encoding="utf-8")
    (tmp_path / "reports" / "r.log").write_text("log-line", encoding="utf-8")
    (tmp_path / "outside.txt").write_text("secret", encoding="utf-8")

    sdk, _, _ = _make_sdk()

    out = sdk["files"].read("data/a.txt")
    assert out["text"] == "hello-data"
    assert out["truncated"] is False

    assert sdk["files"].read("reports/r.log")["text"] == "log-line"

    entries = sdk["files"].list("data")
    assert any(e["name"] == "a.txt" for e in entries)

    for bad in ("outside.txt", "../outside.txt", "data/../outside.txt", "/etc/passwd"):
        with pytest.raises(ValueError):
            sdk["files"].read(bad)
    with pytest.raises(ValueError):
        sdk["files"].list("../")


def test_files_truncation_and_binary(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    big = tmp_path / "data" / "big.txt"
    big.write_text("y" * (1024 * 1024 + 100), encoding="utf-8")
    (tmp_path / "data" / "bin.dat").write_bytes(b"\x00\x01\x02" * 100)

    sdk, _, _ = _make_sdk()
    out = sdk["files"].read("data/big.txt")
    assert out["truncated"] is True
    assert len(out["text"].encode()) <= 1024 * 1024

    with pytest.raises(ValueError):
        sdk["files"].read("data/bin.dat")


# ── platform 命名空间 ──────────────────────────────────────────────────────


def test_platform_binds_identity_and_wraps_tool_queries(monkeypatch):
    captured = {}

    async def fake_count(ctx, project_id=None, **kw):
        captured["user_id"] = ctx.user_id
        captured["project_id"] = project_id
        from core.tools.result import ToolResult

        return ToolResult(ok=True, summary="ok", data={"count": 5})

    from core.tools.builtin import demo

    monkeypatch.setattr(demo, "count_cases", fake_count)

    sdk, _, _ = _make_sdk(user_id=42)
    out = sdk["platform"].count_cases(project_id=24)
    assert captured["user_id"] == 42
    assert captured["project_id"] == 24
    assert out["data"] == {"count": 5}


def test_platform_functions_available():
    sdk, _, _ = _make_sdk()
    for name in ("list_projects", "search_cases", "get_case", "list_suites", "list_batches", "get_batch", "count_cases"):
        assert callable(getattr(sdk["platform"], name))
