# Tasks: 测试管理闭环（计划 / 轮次 / 任务 / 评论 / 评审）

**Input**: `specs/050-test-management-loop/`（spec / plan / research / data-model / contracts）
**Tests**: 单测（无网络）+ 契约（本地 sqlite）+ 前端构建 + 真机验证。

## Phase 1: Setup

- [X] T001 创建特性分支与 spec 产物（spec/plan/research/data-model/contracts/checklists/quickstart）

## Phase 2: Foundational（模型 + 权限）

- [x] T002 新增模型 `app/models/test_plan.py`（TestPlan/TestPlanItem）、`test_cycle.py`（TestCycle/TestCycleItem）、`comment.py`、`case_review.py`
- [x] T003 导出模型 + `app/main.py` 幂等索引 + 路由注册骨架
- [x] T004 权限辅助 `ensure_project_access()` 与项目成员用户查询（admin/project_ids）
- [x] T005 单测 `tests/unit/test_test_plan.py`：进度口径、去重、用例删除、状态流转

## Phase 3: US1 测试计划（P1）

- [x] T006 服务 `app/services/test_plan.py`：CRUD、批量加用例、进度统计、归档/删除约束
- [x] T007 路由 `app/routers/plan_router.py`：`/api/plans` 与 `/api/plans/{id}/items` 全部端点
- [x] T008 契约 `tests/contract/test_plans_api.py`：CRUD、去重、403、409 删除约束、进度
- [x] T009 前端计划列表页（新建/筛选/进度条/状态）
- [x] T010 前端计划详情页（用例表格、批量添加、行内编辑负责人/截止日/状态）

## Phase 4: US2 轮次（P1）

- [x] T011 服务 `app/services/test_cycle.py`：建轮次快照、启动批次、取消、重跑失败
- [x] T012 路由：`/api/plans/{id}/cycles`、`/api/cycles/{id}`、`start/cancel/retry-failed`
- [x] T013 收尾回填 `sync_cycle_from_batch()` 接入 `app/services/batch_finalize.py`
- [x] T014 单测 `tests/unit/test_test_management.py`：状态机、快照、回填、重跑、重复启动
- [x] T015 契约 `tests/contract/test_test_management_api.py`：创建/启动（mock 执行）/详情/403/409
- [x] T016 前端轮次 UI：创建弹窗、轮次列表、开始/重跑/取消、批次跳转

## Phase 5: US3 任务分配与我的待办（P1）

- [x] T017 指派校验 + 指派通知（`notifications`）
- [x] T018 `GET /api/my/tasks` 聚合与筛选（todo/overdue/done）
- [x] T019 单测/契约：跨项目聚合、逾期口径、通知
- [x] T020 前端「我的待办」页（筛选、逾期高亮、跳计划）

## Phase 6: US4 评论（P2）

- [x] T021 服务 `app/services/comments.py`：@ 解析、权限、通知
- [x] T022 路由 `/api/comments`（列表/发表/编辑/删除）
- [x] T023 契约 `tests/contract/test_test_management_api.py`：@ 通知、403、422
- [x] T024 前端用例详情评论 Tab（列表/发表/编辑/删除）

## Phase 7: US5 人工评审（P2）

- [x] T025 服务 `app/services/case_review.py`：状态机、pending 唯一、通知
- [x] T026 路由 `/api/case-reviews` + `/api/cases/{id}/reviews`
- [x] T027 契约 `tests/contract/test_test_management_api.py`：发起/决策/409/权限
- [x] T028 前端：评审发起、待评审队列、用例详情评审状态

## Phase 8: 集成与验证

- [x] T029 路由/菜单/i18n 接入（plans、my-tasks、详情）
- [x] T030 前端 `tsc --noEmit` + `npm run build`
- [x] T031 全量单测 + 契约回归（基线 2161）
- [x] T032 真机验证（计划→轮次→批次→回填→待办→评论→评审）
- [x] T033 构建镜像、提交、推送
