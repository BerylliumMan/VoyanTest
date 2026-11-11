# Implementation Plan: 测试管理闭环（计划 / 轮次 / 任务 / 评论 / 评审）

**Branch**: `050-test-management-loop` | **Date**: 2026-11-11 | **Spec**: [spec.md](./spec.md)
**Input**: Feature specification from `/specs/050-test-management-loop/spec.md`

## Summary

在不改变执行语义的前提下，补上「人」的一层：

1. **计划**：`test_plans` + `test_plan_items`，支持用例批量加入、负责人/截止日/优先级/状态。
2. **轮次**：`test_cycles` + `test_cycle_items`，创建时固化用例快照；启动后创建 `source='test_cycle'` 的批次；收尾回填统计与计划项状态。
3. **待办**：跨项目聚合指派给当前用户的计划项，支持待处理/逾期/已完成筛选。
4. **协作**：`comments`（用例评论 + @ 提及）与 `case_reviews`（人工评审）。

执行仍走 `core.runner.run_batch_test_cases`；批次收尾复用 049 的 `finalize_batch`，在其中追加 `sync_cycle_from_batch`。前端新增计划列表/详情与我的待办，用例详情加评论与评审入口。

## Technical Context

**Language/Version**: Python 3.11 / TypeScript 5（React 18 + Arco Design）
**Primary Dependencies**: FastAPI、SQLAlchemy async、pydantic-settings、React Router、Arco（均已有，无新增三方依赖）
**Storage**: PostgreSQL；6 张新表由 `Base.metadata.create_all` 建立，启动期幂等 DDL 兜底（沿用项目约定）
**Testing**: pytest（unit 无网络、contract 用本地 sqlite）；前端 `tsc --noEmit` + `npm run build`
**Target Platform**: Linux 服务端容器（前端静态资源由 FastAPI 托管）
**Project Type**: Web 服务（FastAPI + React/Arco）
**Performance Goals**: 计划/轮次列表接口 P95 < 200ms（单项目 ≤ 2000 计划项）；我的待办跨项目聚合 < 300ms
**Constraints**: 不改变执行器/失败分类语义；不新增外发通知渠道；不引入迁移框架；不上组织/RBAC
**Scale/Scope**: 单项目 ≤ 200 计划、≤ 50 轮次/计划、≤ 2000 计划项；评论 ≤ 5000 字

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

- **I. API-First**：计划、轮次、任务、评论、评审全部先定义 REST 端点，前端只消费端点。✅
- **II. Test Integrity**：新增单测（状态机/权限/进度）+ 契约测试（sqlite），无网络依赖；全量回归不降级。✅
- **III. Asset Localization**：只用 Arco 与本地构建产物，不引外部 CDN。✅
- **IV. Configuration as Code**：无新增运行配置；如后续加开关，统一进 `app/config.py::Settings`。✅
- **V. Simplicity**：复用 `test_cases`/`run_batches`/`notifications`，不新建服务进程、不引入队列；轮次与批次用 `source/source_id` 弱关联，避免大改执行器。✅

## Project Structure

### Documentation (this feature)

```text
specs/050-test-management-loop/
├── plan.md              # 本文件
├── spec.md              # 需求
├── research.md          # 关键取舍
├── data-model.md        # 表与枚举
├── quickstart.md        # 真机验证步骤
├── contracts/
│   └── test-management.md
├── checklists/
│   └── requirements.md
└── tasks.md
```

### Source (repository)

```text
app/
├── models/
│   ├── test_plan.py           # TestPlan / TestPlanItem
│   ├── test_cycle.py          # TestCycle / TestCycleItem
│   ├── comment.py             # Comment
│   └── case_review.py         # CaseReview
├── services/
│   ├── test_plan.py           # 计划/项 CRUD、进度、权限
│   ├── test_cycle.py          # 轮次创建/启动/停止/重跑/收尾同步
│   ├── comments.py            # 评论、@ 解析、通知
│   └── case_review.py         # 评审状态机、通知
├── routers/
│   ├── plan_router.py         # /api/plans, /api/cycles, /api/my/tasks
│   ├── comment_router.py      # /api/comments
│   └── case_review_router.py  # /api/case-reviews
├── main.py                    # 注册路由 + 幂等 DDL
└── models/__init__.py         # 导出新模型

core/runner/_orchestrator.py   # 不需要改（轮次通过 batch 启动）
app/services/batch_finalize.py # 追加 sync_cycle_from_batch

frontend/src/
├── pages/plans/               # 列表 + 详情（用例 / 轮次）
├── pages/my_tasks/            # 我的待办
├── pages/testcases/           # 评论抽屉 + 评审入口
├── routes.ts                  # 菜单/路由
└── locale/index.ts            # 中英文案

tests/
├── unit/test_test_plan.py
├── unit/test_test_cycle.py
├── unit/test_case_review.py
├── contract/test_plans_api.py
├── contract/test_comments_api.py
└── contract/test_case_reviews_api.py
```

## Phase Plan

- **Phase 0（研究）**：确认表结构、批次关联、收尾同步点、权限口径、评审唯一性；产出 research.md。
- **Phase 1（设计）**：data-model.md、contracts/test-management.md、quickstart.md、checklists。
- **Phase 2（地基）**：模型 + 幂等 DDL + 权限辅助 + 计划/项服务与 API + 单测。
- **Phase 3（轮次）**：轮次快照、启动批次、收尾同步、重跑失败、计划项状态回填 + 测试。
- **Phase 4（待办）**：我的待办聚合 + 逾期筛选 + 指派通知 + 测试。
- **Phase 5（协作）**：评论 + @ 通知；人工评审状态机 + 通知 + 测试。
- **Phase 6（前端）**：计划页、计划详情、我的待办、用例评论/评审；构建。
- **Phase 7（验证）**：全量单测、契约、真机（计划→轮次→批次→回填→评论→评审）、镜像与提交。

## Complexity Tracking

| 取舍 | 原因 |
|---|---|
| 轮次不复用 `QualityRegressionPlan` 表 | 回归计划是「用例集合 + 最近批次」，没有生命周期/快照/统计；硬改会污染 042 的语义。新表通过 `requirement_id`/`plan_id` 弱关联，保留两条路径并存。 |
| 轮次与批次用 `source/source_id` 而非新外键表 | 执行器已经用 `source` 标记来源（如 `api_scenario`），复用即可；避免改 `run_batch_test_cases` 签名和 049 的重试链。 |
| 计划项状态自动同步 + 允许手动 blocked | 自动同步保证结果一致；手动 blocked 表达「已知问题/阻塞」，下一次轮次完成后覆盖，符合测试管理习惯。 |
| 评论用通用 `entity_type/entity_id` | 现在只开放 case，后续计划/轮次/缺陷可复用同一张表与 API，不增加迁移成本。 |
| 评审不做多人会签 | v1 一个评审人；多人会签需要评审组/顺序，留到组织/RBAC 一起做。 |
