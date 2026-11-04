# Tasks: 断言增强与自动补用例鉴权

**Input**: `specs/048-assertion-auth/`（spec.md、plan.md、research.md、data-model.md、contracts/）
**Tests**: 单测（无网络）+ 真机验证。

## Phase 1: Setup

- [X] T001 确认执行器 `jsonschema` 断言可用（`core/api_runner/assertions.py::_jsonschema_check`），无代码改动

## Phase 2: Foundational

- [X] T002 [P] 写登录接口查找公共服务测试 in `tests/unit/test_api_case_auth.py`（POST+password、路径特征、自身需鉴权排除、兜底目标定义）
- [X] T003 新建 `app/services/api_case_auth.py`：`needs_auth` / `login_candidate` / `find_login_definition`
- [X] T004 路由改为复用公共服务 in `app/routers/api_test/generate.py`

## Phase 3: 断言增强（US1，P1）

- [X] T005 [P] [US1] 写断言生成测试 in `tests/unit/test_api_assertions.py`（schema→jsonschema、2xx 范围、无 schema 兜底、负向不变）
- [X] T006 [US1] 新增 `_first_2xx_schema` 并把正向状态码改为范围断言 in `app/gen/api/generator.py`
- [X] T007 [US1] 有 2xx schema 时追加 jsonschema 断言 in `app/gen/api/generator.py`

## Phase 4: 自动补用例鉴权（US2，P2）

- [X] T008 [P] [US2] 补充自动补用例鉴权测试 in `tests/unit/test_api_auto_cases.py`（两步用例、鉴权头、无登录定义提示）
- [X] T009 [US2] 自动补用例接入登录定义查找 in `app/services/api_auto_cases.py`

## Phase 5: 验证与收尾

- [X] T010 全量单测与 046/047 契约回归
- [X] T011 真机验证：结构断言失败可识别；受保护接口自动补出两步用例
- [X] T012 更新文档 in `specs/048-assertion-auth/quickstart.md`（如实现有偏差）

## 依赖

- T003 → T004 → T009
- T006/T007（US1）与 T003-T004（基础）可并行
- T009 依赖 T003 与 T007（生成器能力）

## 并行

- T002 与 T005/T008 测试可并行编写
- T006 与 T003 可并行
