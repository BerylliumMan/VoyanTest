# Research: 断言增强与自动补用例鉴权

## 结论摘要

- 执行器已支持 `jsonschema` 断言（`core/api_runner/assertions.py`），无需扩展。
- 生成器已有 `_auth_requirements` / `_apply_auth_header` / `_login_step`，两层鉴权能力现成；缺的是自动补用例没有传 `auth_definition`。
- 登录接口判定规则与路由内实现一致，需抽到服务层共用。

## 决策

| 主题 | 决策 | 理由 | 备选 |
|---|---|---|---|
| 结构断言 | 复用 `jsonschema` | 覆盖必填/类型/嵌套，零执行器改动 | 逐字段 jsonpath（断言爆炸） |
| 状态码 | 2xx 范围（gte 200 + lt 300） | 兼容 200/201/204，避免误判 | 固定状态码（误判 201） |
| 登录查找 | 抽 `app/services/api_case_auth.py` | 人工与自动一致，避免漂移 | 路由内 import（层次倒置） |
| 鉴权触发 | `request_schema.security` 非空 | 与生成器一致，源码接口不误加 | 运行时探测（成本高） |

## 已知限制

- 登录凭证需在响应体中（Cookie 型登录不支持）。
- 源码提取的接口通常没有 2xx 响应 schema，结构断言只在 OpenAPI 来源的接口生效。
