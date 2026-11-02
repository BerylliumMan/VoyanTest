---
description: "Task list for Java 接口自动提取与变更触发"
---

# Tasks: Java 接口自动提取与变更触发

**Input**: `specs/046-java-api-extraction/`（spec.md、plan.md）
**Tests**: TDD（纯函数与契约测试）；不跑全量 E2E。

## Phase 1: Setup

- [X] T001 [P] 新增依赖 `dulwich`、`paramiko`、`tree-sitter`、`tree-sitter-java` 并锁定版本 in `requirements.txt`
- [X] T002 [P] 新增配置：`api_extract_enabled`、`api_extract_sync_interval_minutes`、`api_sync_confirm_window_minutes=60`、`api_sync_misfire_grace_minutes=10`、`api_change_plan_max_pending=5`、`api_repo_clone_timeout_seconds` in `app/config.py`

## Phase 2: Foundational

- [X] T003 新建 `app/models/api_extract.py`：`CodeRepoBinding`、`ApiSyncRecord`、`ApiChangeTrigger`
- [X] T004 在 `app/db_models.py` 注册新模型；在 `app/main.py` 的 `_ddl` 增加建表与 `api_definitions` 扩列（source_file/source_line/source_symbol/extractor/confidence/confidence_reason/status/manual_override）
- [X] T005 新建 `core/api_extract/base.py`：`ExtractedApi` 数据类 + `Extractor` 协议 + `confidence` 枚举
- [X] T006 新建 `core/api_extract/registry.py`：策略 → 提取器映射（auto/openapi/spring_source）

## Phase 3: 提取器

- [X] T007 [P] 写 OpenAPI 提取器测试 in `tests/unit/test_api_extract_openapi.py`（OpenAPI3、Swagger2、路径参数、operationId）
- [X] T008 实现 `core/api_extract/openapi.py`
- [X] T009 [P] 写 Spring 源码提取器测试 in `tests/unit/test_api_extract_spring.py`（类级+方法级拼接、常量拼接、低可信度标记）
- [X] T010 实现 `core/api_extract/spring_source.py`（tree-sitter-java AST）
- [X] T011 [P] 写技术栈探测测试 in `tests/unit/test_api_extract_detect.py`（pom 依赖、源码注解特征）
- [X] T012 实现 `core/api_extract/detect.py`

## Phase 4: Git 与同步

- [X] T013 [P] 写 Git 拉取测试 in `tests/unit/test_git_sync.py`（本地 repo clone/diff、凭据 URL 构造、超时）
- [X] T014 实现 `core/git_sync.py`（dulwich HTTPS / paramiko SSH，浅克隆，diff --name-only）
- [X] T015 [P] 写同步服务测试 in `tests/unit/test_api_sync.py`（新增/更新/下线/manual_override 保护/幂等）
- [X] T016 实现 `app/services/api_sync.py`
- [X] T017 实现路由 `GET/POST/PUT /api/api-test/repos`、连通性测试、手动同步、同步记录 in `app/routers/api_test/repos.py`

## Phase 5: 变更触发

- [X] T018 [P] 写变更影响测试 in `tests/unit/test_api_change_impact.py`（diff 文件 → 接口 → 用例；无命中不建计划）
- [X] T019 实现 `app/services/api_change_impact.py`
- [X] T020 [P] 写计划状态机测试 in `tests/unit/test_api_change_plans.py`（原子认领、宽限、超期跳过、重试退避、合并、上限）
- [X] T021 实现 `app/services/api_change_plans.py`（claim/confirm/cancel/auto-run/僵尸回收）
- [X] T022 在 `app/scheduler.py` 挂两个定时任务：仓库 HEAD 巡检（5 分钟）、计划到期扫描（30 秒）
- [X] T023 实现路由 `GET /api/api-test/change-plans`、`POST .../confirm`、`POST .../cancel` in `app/routers/api_test/change_plans.py`

## Phase 6: 前端

- [X] T024 仓库绑定面板（地址/分支/凭据类型/同步策略/提取器/连通性测试/手动同步/同步记录）in `frontend/src/pages/api-test/components/RepoBindingPanel.tsx`
- [X] T025 变更计划列表（倒计时、受影响接口、用例数、确认/取消）in `frontend/src/pages/api-test/components/ChangePlanList.tsx`
- [X] T026 接口详情展示来源与可信度 in `frontend/src/pages/api-test/`（定义详情处）

## Phase 7: 验证

- [X] T027 契约测试 in `tests/contract/test_api_repo_sync.py`（权限、凭据打码、幂等、超时）
- [X] T028 跑 `pytest tests/unit/ -q` 与相关契约测试，修复回归
- [X] T029 真实仓库验证：有 OpenAPI 的项目 + 无 OpenAPI 的 Spring 项目各提取一次；一次变更触发闭环

---

## Phase 8: 增量完善（v2）

- [X] T030 后端：接口 patch 支持 `manual_override`（crud + router 模型）in `app/crud/api_definition.py`、`app/routers/api_test/definitions.py`
- [X] T031 [P] 前端：接口详情「人工修正」开关与列表标记 in `frontend/src/pages/api_test/definitions/`
- [X] T032 后端：绑定表新增 `confirm_window_minutes`/`full_sync_every` 与 DDL，计划创建读取项目配置 in `app/models/api_extract.py`、`app/main.py`、`app/services/api_change_plans.py`
- [X] T033 [P] 前端：绑定面板增加确认窗口与全量校准间隔 in `frontend/src/pages/api_test/components/RepoBindingPanel.tsx`
- [X] T034 后端：同步与计划决策写审计日志 in `app/services/api_sync.py`、`app/services/api_change_plans.py`
- [X] T035 后端：同步 `running` 记录 + 取消接口 + 协作式取消检查 in `app/services/api_sync.py`、`app/routers/api_test/repos.py`
- [X] T036 [P] 前端：同步状态展示与取消按钮 in `RepoBindingPanel.tsx`
- [X] T037 后端：增量提取（diff 文件过滤 + 全量校准计数）in `core/api_extract/spring_source.py`、`app/services/api_sync.py`
- [X] T038 后端：大仓库排除规则与扫描上限配置 in `core/api_extract/detect.py`、`core/api_extract/spring_source.py`、`app/config.py`
- [X] T039 后端+前端：手动「同步并检查变更」 in `app/routers/api_test/repos.py`、`RepoBindingPanel.tsx`
- [X] T040 [P] 前端：解绑仓库按钮 in `RepoBindingPanel.tsx`
- [X] T041 [P] 前端：变更计划批次号跳转报告 in `ChangePlanList.tsx`
- [X] T042 [P] 前端：同步记录与变更计划筛选/分页 in `ChangePlanList.tsx`、`RepoBindingPanel.tsx`
- [X] T043 后端：计划生成与自动执行站内通知 in `app/services/api_change_plans.py`
- [X] T044 [P] 文档：quickstart / data-model / contracts in `specs/046-java-api-extraction/`
- [X] T045 测试与真实验证：单测、契约、Web 真机覆盖率与增量耗时

## 依赖

- T003-T006 先于 Phase 3/4/5
- T013-T017 先于 T018-T023
- T024-T026 依赖 T017/T023 的接口
- T029 最后

## 并行

- Phase 3 的 T007/T009/T011 三个测试可并行
- Phase 4 的 T013/T015 可并行
