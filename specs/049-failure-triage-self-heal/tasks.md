# Tasks: 失败分类与自愈

**Input**: `specs/049-failure-triage-self-heal/`（spec / plan / research / data-model / contracts）
**Tests**: 单测（无网络）+ 契约（本地 sqlite）+ 真机验证。

## Phase 1: Setup

- [X] T001 创建特性分支与 spec 产物（spec/plan/research/data-model/contracts/checklists/quickstart）

## Phase 2: Foundational（分类器 + 落库）

- [X] T002 [P] 写分类单测 `tests/unit/test_failure_triage.py`（12 组样本、优先级、空值、脱敏）
- [X] T003 实现 `app/services/failure_triage.py`：`classify_api_failure` / `classify_ui_failure` / `classify_report` / `summarize_runs` / `notify_verdict`
- [X] T004 模型加列（`TestRun.failure_kind/reason`、`RunBatch.auto_retry/failure_summary`）+ 启动幂等 DDL + 复合索引
- [X] T005 落库接入：`save_test_run_result()` 与 `mark_run_failed()` 写分类

## Phase 3: US1 类别可见（P1）

- [X] T006 [P] 契约测试 `tests/contract/test_failure_summary_api.py`（汇总端点、详情筛选、历史空值）
- [X] T007 实现 `GET /api/reports/batches/{id}/failure-summary`
- [X] T008 批次详情返回 `failure_kind/failure_reason`，支持 `failure_kind` 查询参数
- [X] T009 前端报告页：类别列 + 筛选 + 详情原因展示

## Phase 4: US2 自动重试（P2）

- [X] T010 [P] 单测 `tests/unit/test_failure_retry.py`（开关、类别白名单、上限截断、幂等、轮次、取消/运行中不触发）
- [X] T011 「仅失败重跑」下沉为 `app/services/batch_rerun.py::create_rerun_batch()`，路由改为复用并支持 `kinds`
- [X] T012 实现 `app/services/failure_retry.py::maybe_schedule_retry()`（幂等 + 限流 + 子批次标记）
- [X] T013 实现 `app/services/batch_finalize.py::finalize_batch()`：等待结束 → 汇总 → 重试 → 通知
- [X] T014 三处收尾改为调用 `finalize_batch`（接口执行 / 客户端桥接 / 重跑路由）

## Phase 5: US3/US4 通知与展示（P2/P3）

- [X] T015 通知按分类分级：`notify_verdict()` 接入 `notifications`，文案含分类计数与重试说明
- [X] T016 前端：批次摘要分布、自动重试徽标与跳转

## Phase 6: 验证与收尾

- [X] T017 开关关闭回归：分类仍可读、无重试、通知回到旧行为
- [X] T018 全量单测 + 046/047/048 契约回归
- [X] T019 真机验证（环境/断言/鉴权三类样本）并记录证据
- [X] T020 更新文档与提交（按仓库惯例待用户指示）

## Phase 7: 复核补强（2026-11-04）

- [X] T021 分类器：失败断言优先、状态码压过纯文本关键词；多步用例取失败步骤状态码
- [X] T022 重试幂等：部分唯一索引 `ux_run_batches_auto_retry_parent` + 冲突回查；重试批次仍环境失败升级 `error`
- [X] T023 性能与展示：报告解析移入线程池；报告页筛选空态、摘要仅终态请求
- [X] T024 集成测试 `tests/unit/test_batch_finalize.py`（派生/幂等/升级/开关）

## 依赖

- T002 → T003 → T005
- T004 → T005/T007
- T011 → T012 → T013 → T014
- T015 依赖 T003 的 `notify_verdict`

## 并行

- T002 与 T006/T010 可并行编写
- T007 与 T012 可并行（读写在同批次不同文件）
