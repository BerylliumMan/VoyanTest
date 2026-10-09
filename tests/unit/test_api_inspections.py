"""US1 体检规则矩阵（inspect_api_spec）——033-api-test-maturity。

TDD：先于实现编写（预期红：core.api_inspections 尚不存在）。
"""

from __future__ import annotations

from core.api_inspections import Finding, inspect_api_spec


# ── 构造器 ──────────────────────────────────────────────────────────────────

def _req(**over):
    req = {
        "method": "GET",
        "url": "https://api.example.com/items",
        "headers": [],
        "query": [],
        "body": {"type": "none", "content": "", "form_data": [], "files": []},
        "auth": {"type": "none"},
    }
    req.update(over)
    return req


def _step(**over):
    s = {
        "order": 1,
        "name": "",
        "enable": True,
        "request": _req(),
        "assertions": [],
        "extractors": [],
    }
    s.update(over)
    return s


def _spec(*steps, variables=None, dataset_id=None):
    return {
        "schema_version": 1,
        "variables": variables or [],
        "steps": list(steps) or [_step()],
        "dataset_id": dataset_id,
    }


def _env(base_url="", variables=None, services=None):
    return {
        "base_url": base_url,
        "variables": variables or [],
        "services": services or {},
    }


def _codes(findings, code):
    return [f for f in findings if f.code == code]


# ── unresolved_variable ─────────────────────────────────────────────────────

def test_missing_variable_reported_with_location():
    spec = _spec(_step(request=_req(url="https://api.example.com/u/{{token}}")))
    out = inspect_api_spec(spec, environment=_env(), dataset=None)
    f = _codes(out, "unresolved_variable")
    assert len(f) == 1
    assert f[0].level == "error"
    assert "token" in f[0].message
    assert f[0].message == "变量 {{token}} 未解析（步骤 1 的 request.url）"
    assert f[0].step_index == 0
    assert f[0].field == "request.url"


def test_case_env_and_dataset_variables_are_available():
    spec = _spec(
        _step(request=_req(url="{{base}}/x", headers=[{"key": "X-T", "value": "{{token}}", "enable": True}])),
        variables=[{"key": "base", "value": "https://a.example.com", "enable": True}],
        dataset_id=1,
    )
    dataset = {"columns": ["token"], "rows": [["t1"]]}
    out = inspect_api_spec(spec, environment=_env(variables=[{"key": "extra", "value": "1"}]), dataset=dataset)
    assert _codes(out, "unresolved_variable") == []


def test_env_base_url_provides_baseurl_variable():
    spec = _spec(_step(request=_req(url="{{baseUrl}}/v1")))
    assert _codes(inspect_api_spec(spec, environment=_env(base_url="https://x.example.com"), dataset=None), "unresolved_variable") == []
    # 无 base_url → unresolved（含提示）
    out = inspect_api_spec(spec, environment=_env(), dataset=None)
    f = _codes(out, "unresolved_variable")
    assert len(f) == 1 and "baseUrl" in f[0].message


def test_builtin_variables_are_available():
    spec = _spec(_step(request=_req(url="https://x/{{$uuid}}?t={{$timestamp}}")))
    assert _codes(inspect_api_spec(spec, environment=_env(), dataset=None), "unresolved_variable") == []


def test_unknown_dollar_variable_is_unresolved():
    spec = _spec(_step(request=_req(url="https://x/{{$not_a_builtin}}")))
    f = _codes(inspect_api_spec(spec, environment=_env(), dataset=None), "unresolved_variable")
    assert len(f) == 1


def test_extractor_output_counts_as_available():
    spec = _spec(
        _step(extractors=[{"type": "jsonpath", "expression": "$.data.id", "variable": "uid", "scope": "case"}]),
        _step(request=_req(url="https://x/{{uid}}")),
    )
    assert _codes(inspect_api_spec(spec, environment=_env(), dataset=None), "unresolved_variable") == []


def test_script_assignment_counts_as_available():
    spec = _spec(
        _step(pre_script="stamp = 123\nother = stamp + 1", post_script=""),
        _step(request=_req(url="https://x/{{stamp}}")),
    )
    assert _codes(inspect_api_spec(spec, environment=_env(), dataset=None), "unresolved_variable") == []


def test_spec_level_variable_values_are_scanned():
    spec = _spec(
        _step(),
        variables=[{"key": "a", "value": "{{missing_thing}}", "enable": True}],
    )
    f = _codes(inspect_api_spec(spec, environment=_env(), dataset=None), "unresolved_variable")
    assert len(f) == 1 and f[0].step_index == -1


# ── url_empty / url_relative_without_base ───────────────────────────────────

def test_empty_url_is_error():
    spec = _spec(_step(request=_req(url="")))
    f = _codes(inspect_api_spec(spec, environment=_env(), dataset=None), "url_empty")
    assert len(f) == 1 and f[0].level == "error"


def test_relative_url_without_base_warns():
    spec = _spec(_step(request=_req(url="/v1/items")))
    f = _codes(inspect_api_spec(spec, environment=_env(), dataset=None), "url_relative_without_base")
    assert len(f) == 1 and f[0].level == "warning"


def test_relative_url_with_base_is_ok():
    spec = _spec(_step(request=_req(url="/v1/items")))
    out = inspect_api_spec(spec, environment=_env(base_url="https://x.example.com"), dataset=None)
    assert _codes(out, "url_relative_without_base") == []


def test_deactivated_step_is_skipped():
    spec = _spec(_step(enable=False, request=_req(url="")))
    assert _codes(inspect_api_spec(spec, environment=_env(), dataset=None), "url_empty") == []


# ── headers / duplicates ────────────────────────────────────────────────────

def test_header_empty_value_warns():
    spec = _spec(_step(request=_req(headers=[{"key": "X-A", "value": "", "enable": True}])))
    f = _codes(inspect_api_spec(spec, environment=_env(), dataset=None), "header_empty_value")
    assert len(f) == 1 and f[0].level == "warning" and "X-A" in f[0].message


def test_disabled_header_is_skipped():
    spec = _spec(_step(request=_req(headers=[{"key": "X-A", "value": "", "enable": False}])))
    assert _codes(inspect_api_spec(spec, environment=_env(), dataset=None), "header_empty_value") == []


def test_duplicate_header_keys_case_insensitive():
    spec = _spec(_step(request=_req(headers=[
        {"key": "X-Trace", "value": "1", "enable": True},
        {"key": "x-trace", "value": "2", "enable": True},
    ])))
    f = _codes(inspect_api_spec(spec, environment=_env(), dataset=None), "duplicate_keys")
    assert len(f) == 1 and "X-Trace" in f[0].message


def test_duplicate_query_keys():
    spec = _spec(_step(request=_req(query=[
        {"key": "page", "value": "1", "enable": True},
        {"key": "page", "value": "2", "enable": True},
    ])))
    assert len(_codes(inspect_api_spec(spec, environment=_env(), dataset=None), "duplicate_keys")) == 1


# ── body ────────────────────────────────────────────────────────────────────

def test_body_json_with_placeholder_is_valid():
    spec = _spec(_step(request=_req(method="POST", body={
        "type": "json", "content": '{"id": {{uid}}, "name": "{{user}}"}', "form_data": [], "files": [],
    })))
    out = inspect_api_spec(spec, environment=_env(variables=[{"key": "uid", "value": "1"}, {"key": "user", "value": "u"}]), dataset=None)
    assert _codes(out, "body_json_invalid") == []


def test_body_json_invalid_is_error():
    spec = _spec(_step(request=_req(method="POST", body={"type": "json", "content": '{"a": ', "form_data": [], "files": []})))
    f = _codes(inspect_api_spec(spec, environment=_env(), dataset=None), "body_json_invalid")
    assert len(f) == 1 and f[0].level == "error"


def test_content_type_mismatch_warns():
    spec = _spec(_step(request=_req(method="POST", headers=[{"key": "Content-Type", "value": "text/plain", "enable": True}], body={"type": "json", "content": "{}", "form_data": [], "files": []})))
    f = _codes(inspect_api_spec(spec, environment=_env(), dataset=None), "content_type_mismatch")
    assert len(f) == 1 and f[0].level == "warning"


def test_content_type_matching_json_is_ok():
    spec = _spec(_step(request=_req(method="POST", headers=[{"key": "Content-Type", "value": "application/json; charset=utf-8", "enable": True}], body={"type": "json", "content": "{}", "form_data": [], "files": []})))
    assert _codes(inspect_api_spec(spec, environment=_env(), dataset=None), "content_type_mismatch") == []


def test_content_type_absent_is_ok():
    spec = _spec(_step(request=_req(method="POST", body={"type": "json", "content": "{}", "form_data": [], "files": []})))
    assert _codes(inspect_api_spec(spec, environment=_env(), dataset=None), "content_type_mismatch") == []


# ── extractors ──────────────────────────────────────────────────────────────

def test_jsonpath_syntax_error():
    spec = _spec(_step(extractors=[{"type": "jsonpath", "expression": "$.data[", "variable": "x", "scope": "case"}]))
    f = _codes(inspect_api_spec(spec, environment=_env(), dataset=None), "jsonpath_syntax")
    assert len(f) == 1 and f[0].level == "error"


def test_jsonpath_valid_is_ok():
    spec = _spec(_step(extractors=[{"type": "jsonpath", "expression": "$.data.items[0].id", "variable": "x", "scope": "case"}]))
    assert _codes(inspect_api_spec(spec, environment=_env(), dataset=None), "jsonpath_syntax") == []


# ── secret_in_url ───────────────────────────────────────────────────────────

def test_secret_variable_in_url_warns():
    spec = _spec(
        _step(request=_req(url="https://x/cb?token={{api_token}}")),
        variables=[{"key": "api_token", "value": "s3", "secret": True, "enable": True}],
    )
    f = _codes(inspect_api_spec(spec, environment=_env(), dataset=None), "secret_in_url")
    assert len(f) == 1 and f[0].level == "warning" and "api_token" in f[0].message


def test_env_secret_in_url_warns():
    spec = _spec(_step(request=_req(url="https://x/{{token}}")))
    env = _env(variables=[{"key": "token", "value": "***", "secret": True, "enable": True}])
    assert len(_codes(inspect_api_spec(spec, environment=env, dataset=None), "secret_in_url")) == 1


def test_non_secret_in_url_is_ok():
    spec = _spec(
        _step(request=_req(url="https://x/u/{{user}}")),
        variables=[{"key": "user", "value": "u", "enable": True}],
    )
    assert _codes(inspect_api_spec(spec, environment=_env(), dataset=None), "secret_in_url") == []


# ── dataset ─────────────────────────────────────────────────────────────────

def test_dataset_missing_when_bound_but_not_provided():
    spec = _spec(_step(), dataset_id=9)
    f = _codes(inspect_api_spec(spec, environment=_env(), dataset=None), "dataset_missing_or_mismatch")
    assert len(f) == 1 and f[0].level == "error"


def test_dataset_empty_columns_is_error():
    spec = _spec(_step(), dataset_id=9)
    f = _codes(inspect_api_spec(spec, environment=_env(), dataset={"columns": [], "rows": []}), "dataset_missing_or_mismatch")
    assert len(f) == 1


def test_dataset_ok_no_finding():
    spec = _spec(_step(), dataset_id=9)
    out = inspect_api_spec(spec, environment=_env(), dataset={"columns": ["a"], "rows": [["1"]]})
    assert _codes(out, "dataset_missing_or_mismatch") == []


# ── 零误报 / 结构 ───────────────────────────────────────────────────────────

def test_fully_valid_spec_has_zero_findings():
    spec = _spec(
        _step(
            request=_req(
                method="POST",
                url="{{baseUrl}}/orders",
                headers=[{"key": "Authorization", "value": "Bearer {{token}}", "enable": True}],
                body={"type": "json", "content": '{"sku": "{{sku}}"}', "form_data": [], "files": []},
            ),
            assertions=[{"type": "status_code", "expected": 200}],
            extractors=[{"type": "jsonpath", "expression": "$.data.id", "variable": "order_id", "scope": "case"}],
        ),
        variables=[{"key": "token", "value": "t", "secret": True, "enable": True}],
    )
    env = _env(base_url="https://api.example.com", variables=[{"key": "sku", "value": "S1"}])
    out = inspect_api_spec(spec, environment=env, dataset=None)
    assert out == [], [f.to_dict() for f in out]


def test_findings_sorted_and_serializable():
    spec = _spec(
        _step(request=_req(url="/rel", headers=[{"key": "X-A", "value": "", "enable": True}])),
        _step(request=_req(url="https://x/{{missing}}")),
    )
    out = inspect_api_spec(spec, environment=_env(), dataset=None)
    assert [f.step_index for f in out] == sorted(f.step_index for f in out)
    d = out[0].to_dict()
    assert set(d.keys()) == {"level", "code", "message", "step_index", "field", "hint"}
    assert isinstance(out[0], Finding)
