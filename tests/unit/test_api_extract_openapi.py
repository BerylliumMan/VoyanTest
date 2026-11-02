"""046：OpenAPI 提取器测试。"""

from core.api_extract.openapi import OpenApiExtractor
from core.api_extract.base import ExtractContext

OPENAPI3 = {
    "openapi": "3.0.1",
    "info": {"title": "Demo", "version": "1"},
    "paths": {
        "/users": {
            "get": {"operationId": "listUsers", "summary": "用户列表",
                    "parameters": [{"name": "page", "in": "query", "schema": {"type": "integer"}}]},
            "post": {"operationId": "createUser", "summary": "创建用户",
                     "requestBody": {"content": {"application/json": {
                         "schema": {"type": "object", "properties": {"name": {"type": "string"}}}}}}},
        },
        "/users/{id}": {
            "get": {"operationId": "getUser", "summary": "用户详情",
                    "parameters": [{"name": "id", "in": "path", "required": True,
                                    "schema": {"type": "string"}}]},
        },
    },
}

SWAGGER2 = {
    "swagger": "2.0",
    "info": {"title": "Legacy", "version": "1"},
    "basePath": "/api",
    "paths": {
        "/orders": {
            "post": {"operationId": "createOrder", "summary": "下单",
                     "parameters": [{"name": "body", "in": "body",
                                     "schema": {"type": "object", "properties": {"qty": {"type": "integer"}}}}]},
        }
    },
}


def test_openapi3_extracts_all_operations():
    apis = OpenApiExtractor().extract(ExtractContext(openapi_doc=OPENAPI3))
    got = {(a.method, a.path) for a in apis}
    assert got == {("GET", "/users"), ("POST", "/users"), ("GET", "/users/{id}")}
    assert all(a.extractor == "openapi" and a.confidence == "high" for a in apis)
    get_user = next(a for a in apis if a.path == "/users/{id}")
    assert get_user.operation_id == "getUser"
    assert get_user.request_schema  # 参数被解析


def test_swagger2_extracts_and_keeps_path_without_basepath():
    apis = OpenApiExtractor().extract(ExtractContext(openapi_doc=SWAGGER2))
    assert [(a.method, a.path) for a in apis] == [("POST", "/orders")]
    assert apis[0].request_schema.get("body")


def test_extractor_requires_document():
    import pytest
    from core.api_extract.base import ExtractorError

    with pytest.raises(ExtractorError):
        OpenApiExtractor().extract(ExtractContext())


def test_openapi_paths_are_normalized():
    doc = {"openapi": "3.0.1", "paths": {"users/": {"get": {"summary": "x"}}}}
    apis = OpenApiExtractor().extract(ExtractContext(openapi_doc=doc))
    assert apis[0].path == "/users"
    assert apis[0].method == "GET"
