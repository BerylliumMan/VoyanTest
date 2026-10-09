"""US5 请求预览（dry_run 全步骤 + 变量轨迹 + unresolved + auth_plan）——033。

TDD：先于实现编写（预期红：扩展字段尚未实现）。
"""

from __future__ import annotations

import asyncio
import json

from app.routers.api_test.debug import _render_only


def _step(url="https://api.example.com/items", **over):
    req = {
        "method": "GET",
        "url": url,
        "headers": [],
        "query": [],
        "body": {"type": "none", "content": ""},
        "auth": {"type": "none"},
    }
    req.update(over)
    return {"order": 1, "name": "", "enable": True, "request": req, "assertions": [], "extractors": []}


def _env(**over):
    env = {"base_url": "", "variables": [], "services": {}, "headers": []}
    env.update(over)
    return env


def _preview(spec, env=None, case_vars=None):
    return asyncio.run(_render_only(spec, env or _env(), case_vars or []))


def test_multi_step_preview_returns_all_steps():
    spec = {"variables": [], "steps": [
        _step(url="https://a.example.com/1"),
        _step(url="https://a.example.com/2"),
    ]}
    out = _preview(spec)
    assert len(out["rendered_steps"]) == 2
    # 向后兼容：rendered 仍是第一步对象（旧调试消费方）
    assert out["rendered"]["url"] == "https://a.example.com/1"
    assert out["rendered_steps"][1]["url"] == "https://a.example.com/2"


def test_secret_masked_in_trace_and_headers():
    spec = {"variables": [], "steps": [
        _step(url="{{baseUrl}}/x", headers=[{"key": "Authorization", "value": "Bearer {{token}}", "enable": True}]),
    ]}
    env = _env(base_url="https://a.example.com", variables=[
        {"key": "token", "value": "s3cr3t", "secret": True, "enable": True},
    ])
    out = _preview(spec, env)
    blob = json.dumps(out, ensure_ascii=False)
    assert "s3cr3t" not in blob  # 渲染头与轨迹都不泄露
    trace = {t["name"]: t for t in out["variables_trace"]}
    assert trace["token"]["scope"] == "environment" and trace["token"]["secret"] is True
    assert trace["token"]["value"] == "******"
    assert trace["baseUrl"]["scope"] == "baseUrl"


def test_unresolved_has_location():
    spec = {"variables": [], "steps": [
        _step(url="https://x/{{missing1}}", body={"type": "json", "content": '{"a": "{{missing2}}"}'}),
    ]}
    out = _preview(spec)
    got = {(u["name"], u["step_index"], u["field"]) for u in out["unresolved"]}
    assert ("missing1", 0, "request.url") in got
    assert ("missing2", 0, "request.body.content") in got
    assert out["error"]


def test_auth_plan_environment_default_oauth2():
    spec = {"variables": [], "steps": [_step(auth={"type": "inherit"})]}
    env = _env(default_auth={"type": "oauth2", "oauth2": {
        "grant": "client_credentials", "token_url": "http://t", "client_id": "c",
        "token_var": "my_token",
    }})
    out = _preview(spec, env)
    assert out["auth_plan"] == {"type": "oauth2", "source": "environment", "token_var": "my_token"}


def test_auth_plan_step_explicit():
    spec = {"variables": [], "steps": [_step(auth={"type": "bearer", "token": "t"})]}
    out = _preview(spec)
    assert out["auth_plan"]["type"] == "bearer" and out["auth_plan"]["source"] == "step"


def test_auth_plan_none_when_all_inherit():
    spec = {"variables": [], "steps": [_step(auth={"type": "inherit"})]}
    out = _preview(spec)
    assert out["auth_plan"]["type"] == "none"


def test_oauth2_step_renders_with_placeholder_not_error():
    spec = {"variables": [], "steps": [_step(auth={
        "type": "oauth2", "oauth2": {"grant": "client_credentials", "token_url": "http://t", "client_id": "c"},
    })]}
    out = _preview(spec)
    assert "error" not in out["rendered_steps"][0] or out["rendered_steps"][0].get("error") is None
    assert out["auth_plan"]["type"] == "oauth2"


def test_builtin_variable_trace():
    spec = {"variables": [], "steps": [_step(url="https://x/{{$uuid}}")]}
    out = _preview(spec)
    trace = {t["name"]: t for t in out["variables_trace"]}
    assert trace["$uuid"]["scope"] == "builtin" and trace["$uuid"]["value"]
    assert out["unresolved"] == []


def test_backward_compat_keys_present():
    spec = {"variables": [], "steps": [_step()]}
    out = _preview(spec)
    for key in ("rendered", "response", "assertions", "extracted", "error"):
        assert key in out
