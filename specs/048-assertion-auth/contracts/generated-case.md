# Contract: 生成的接口用例结构（048 增量）

对外无新增端点；本契约描述生成产物中**新增/变更**的字段。

## 正向用例断言（`api_spec.steps[].assertions`）

必须包含（顺序不固定）：

```json
[
  {"type": "status_code", "condition": "gte", "expected": "200", "name": "状态码 2xx（>=200）"},
  {"type": "status_code", "condition": "lt",  "expected": "300", "name": "状态码 2xx（<300）"}
]
```

若接口定义带 2xx 响应 schema，追加：

```json
{"type": "jsonschema", "condition": "equals", "expected": "",
 "expression": "<JSON Schema 文本>", "name": "响应符合 JSON Schema"}
```

## 负向用例

保持两条：`status_code gte 400`、`status_code lt 500`。

## 鉴权用例（需要鉴权且找到登录定义）

- `steps` 长度 2：
  1. 登录步骤（`order=1`），带 `extractors` 提取 token
  2. 目标步骤（`order=2`），请求头引用 `{{token}}`
- 找不到登录定义：`steps` 长度 1，`notes` 含「需在环境变量中提供 token」。

## 兼容性

- 执行器已支持 `jsonschema` 与两步执行；无破坏性变更。
- 前端用例预览按步骤渲染，无需改动。
