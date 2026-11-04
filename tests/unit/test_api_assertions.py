"""048：断言增强测试（状态码范围 + 响应结构）。"""

from app.gen.api.generator import generate_cases_for_definition

SCHEMA = {
    "type": "object",
    "properties": {"id": {"type": "integer"}, "name": {"type": "string"}},
    "required": ["id", "name"],
}


def _definition(**kw):
    base = {
        "id": 1,
        "name": "订单详情",
        "method": "GET",
        "path": "/orders/{id}",
        "request_schema": {},
        "response_schema": {"statuses": {"200": {"schema": SCHEMA}}},
    }
    base.update(kw)
    return base


def _assertions(definition, index=0):
    cases = generate_cases_for_definition(definition)
    return cases[index]["api_spec"]["steps"][-1]["assertions"]


def test_status_assertions_cover_2xx_range():
    asserts = _assertions(_definition())
    status = [a for a in asserts if a["type"] == "status_code"]
    assert {a["condition"] for a in status} == {"gte", "lt"}
    assert {str(a["expected"]) for a in status} == {"200", "300"}


def test_schema_adds_jsonschema_assertion():
    asserts = _assertions(_definition())
    schema_asserts = [a for a in asserts if a["type"] == "jsonschema"]
    assert len(schema_asserts) == 1
    assert "required" in schema_asserts[0]["expression"]


def test_no_schema_keeps_original_behavior():
    definition = _definition(response_schema={})
    asserts = _assertions(definition)
    assert not [a for a in asserts if a["type"] == "jsonschema"]
    assert [a for a in asserts if a["type"] == "status_code"]


def test_jsonpath_fallback_with_example_only():
    definition = _definition(response_schema={"statuses": {"200": {"example": {"id": 1}}}})
    asserts = _assertions(definition)
    assert not [a for a in asserts if a["type"] == "jsonschema"]
    jsonpath = [a for a in asserts if a["type"] == "jsonpath"]
    assert jsonpath and jsonpath[0]["expression"] == "$.id"


def test_negative_cases_still_expect_4xx():
    from app.gen.api.generator import _build_assertions

    asserts = _build_assertions({"response_schema": {}}, with_jsonpath=False, negative=True)
    assert [a["condition"] for a in asserts] == ["gte", "lt"]
    assert [str(a["expected"]) for a in asserts] == ["400", "500"]
