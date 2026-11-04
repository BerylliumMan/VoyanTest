# Implementation Plan: 断言增强与自动补用例鉴权

**Branch**: `048-assertion-auth` | **Date**: 2026-11-04 | **Spec**: [spec.md](./spec.md)
**Input**: Feature specification from `/specs/048-assertion-auth/spec.md`

## Summary

提升自动生成接口用例的可信度与可执行性：

1. **断言增强**：有 2xx 响应 schema 时，用执行器已有的 `jsonschema` 断言校验整个响应体；状态码断言改为 2xx 范围（≥200 且 <300）。
2. **鉴权接入**：自动补用例时查找项目内登录接口定义，生成「登录 → 调用」两步用例并注入 token；登录接口判定规则抽为公共服务，人工生成与自动补共用。

不新增表、不新增对外 API、不新增配置项。

## Technical Context

**Language/Version**: Python 3.11
**Primary Dependencies**: FastAPI、SQLAlchemy async；执行器已内置 `jsonschema` 断言（无需新依赖）
**Storage**: PostgreSQL（无模型变更）
**Testing**: pytest（单测无网络；契约测试用本地 sqlite）
**Target Platform**: Linux 服务端容器
**Project Type**: Web 服务（后端 + React 前端；本特性只改后端生成逻辑）
**Constraints**: 生成耗时增加可忽略（纯字符串/JSON 拼装）；不得改变负向用例行为
**Scale/Scope**: 单次生成 ≤200 接口；登录定义查找 ≤500 条

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

- **I. API-First**：不新增端点；生成结果仍通过既有用例/草案 API 暴露。✅
- **II. Test Integrity**：新增单测全部本地可跑（无网络）；执行器断言已有单测。✅
- **III. Asset Localization**：无前端资源变更。✅
- **IV. Configuration as Code**：不新增配置；复用 `Settings`。✅
- **V. Simplicity**：复用执行器 `jsonschema` 与既有登录判定，不引入新框架。✅

## Project Structure

### Documentation (this feature)

```text
specs/048-assertion-auth/
├── plan.md
├── research.md
├── data-model.md
├── quickstart.md
├── contracts/
└── tasks.md
```

### Source Code

```text
app/gen/api/generator.py            # 断言生成
app/services/api_case_auth.py       # 登录接口查找（新，公共服务）
app/services/api_auto_cases.py      # 自动补用例接入鉴权
app/routers/api_test/generate.py    # 改为复用公共服务
tests/unit/test_api_assertions.py   # 断言增强单测（新）
tests/unit/test_api_auto_cases.py   # 鉴权接入单测（补充）
```

## Phase 0: Research

### 决策 1：结构断言用 `jsonschema` 而非逐字段断言

- **Decision**：直接把 2xx response schema 作为 `jsonschema` 断言的 schema。
- **Rationale**：执行器已支持该类型（`core/api_runner/assertions.py::_jsonschema_check`），能覆盖必填、类型、嵌套结构；无需扩展执行器。
- **Alternatives**：逐字段生成 `jsonpath` 断言（字段多时断言爆炸、维护成本高）；`expression` 脚本（能力过强、易误用）。

### 决策 2：状态码改为范围断言

- **Decision**：正向用例生成两条断言 `status_code gte 200` 与 `status_code lt 300`。
- **Rationale**：创建类接口常返回 201/204；硬编码 200 会把正确响应判错。与负向用例（400–499）风格一致。
- **Alternatives**：按 `_expected_status` 保留具体码（会误判 201）；增加 `in` 条件（需扩展执行器）。

### 决策 3：登录接口查找抽为公共服务

- **Decision**：把 `_login_candidate` / `_find_login_definition` 迁到 `app/services/api_case_auth.py`，路由与自动补用例共用。
- **Rationale**：FR-007 要求同一套规则；避免两处逻辑漂移。
- **Alternatives**：自动补用例 import 路由模块（层次倒置、易循环依赖）。

### 决策 4：鉴权判定以 `request_schema.security` 为准

- **Decision**：接口声明了 security 才走登录流程；否则单步。
- **Rationale**：与现有生成器 `_auth_requirements` 一致；源码提取的接口通常没有 security，不会误加登录。
- **Alternatives**：按状态码探测（需先执行一次，成本高）。

## Phase 1: Design

### 断言生成（`_build_assertions`，正向分支）

```text
1) status_code gte 200  "状态码 2xx（>=200）"
2) status_code lt 300   "状态码 2xx（<300）"
3) 若存在 2xx schema:
     jsonschema expression=<schema JSON>  "响应符合 JSON Schema"
   否则若响应示例存在:
     jsonpath $.firstField exists          "响应包含 $.firstField"
```

负向分支保持原样（400–499）。

### 自动补用例鉴权流程

```text
plan_after_sync
  → auto_generate_missing_cases(definition_ids)
      1. 一次性查登录定义（项目内，最多 500 条）
      2. 对每个缺失用例的接口：
           needs_auth? → generate_cases_for_definition(auth_definition=login)
           否则       → 单步生成
      3. 登录步骤由生成器写入 extractor，token 注入到目标请求
```

### 数据模型

无新表；仅复用 `test_cases.api_spec` 中步骤结构（断言数组、extractors）。

### Contracts

无新增对外 API。生成产物契约（`api_spec.steps[].assertions`）新增 `jsonschema` 类型，
执行侧已支持；`steps` 可能为两步（登录 + 调用），既有契约校验允许。

## Risks

| 风险 | 对策 |
|---|---|
| 状态码范围改变既有生成结果 | 全量单测回归；断言只放宽不放宽语义（2xx 仍是成功） |
| 响应 schema 与实际不符 | 该用例会失败，属正确反馈；人工可关闭该断言 |
| 登录接口返回 Cookie 而非 token | 记为已知限制，本期不处理 |
| 自动补用例生成两步导致执行时间增加 | 仅需鉴权的接口触发，单次上限 5 个接口 |

## Acceptance

- 单测覆盖：schema→jsonschema、范围断言、负向不变、自动补用例两步与无登录定义提示
- 契约通过；真机验证：受保护接口自动补出两步用例，登录成功时执行通过
