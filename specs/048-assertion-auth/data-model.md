# Data Model: 断言增强与自动补用例鉴权

本特性不新增数据表，仅使用既有结构。

## 接口定义（api_definitions）

| 字段 | 用途 |
|---|---|
| request_schema.security | 判断是否需要鉴权（非空即走登录流程） |
| request_schema.body | 登录接口判定（含 password 字段） |
| response_schema.statuses["2xx"].schema | 生成结构断言的来源 |

## 接口用例步骤（test_cases.api_spec.steps[]）

```json
{
  "order": 1,
  "name": "登录",
  "request": {"method": "POST", "url": "{{baseUrl}}/api/auth/login", "body": {"type": "json", "content": "..."}},
  "assertions": [
    {"type": "status_code", "condition": "gte", "expected": "200"},
    {"type": "status_code", "condition": "lt", "expected": "300"}
  ],
  "extractors": [{"name": "token", "type": "jsonpath", "expression": "$.token"}]
}
```

```json
{
  "order": 2,
  "name": "目标调用",
  "request": {"headers": [{"name": "Authorization", "value": "Bearer {{token}}"}]},
  "assertions": [
    {"type": "status_code", "condition": "gte", "expected": "200"},
    {"type": "status_code", "condition": "lt", "expected": "300"},
    {"type": "jsonschema", "expression": "{\"type\":\"object\",...}", "condition": "equals"}
  ]
}
```

## 关系

- 登录步骤的提取器 → 目标步骤请求头的变量引用（`{{token}}`）。
- 自动补用例：`api_spec.meta.auto_generated=true` 且步骤数可能为 2。
