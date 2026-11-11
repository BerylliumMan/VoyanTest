# Research: 测试管理闭环

## R1. 为什么不扩展现有 `QualityRegressionPlan`

042 的 `quality_regression_plans` 是「用例集合 + 环境 + 最近批次」，用于质量治理的回归建议；它没有：

- 计划生命周期（draft/active/completed/archived）与起止日期；
- 计划项（负责人/截止日/优先级/状态/备注）；
- 轮次快照与多次执行历史；
- 进度口径与任务归属。

硬改会让 042 的「回归计划」语义漂移，且质量页与测试管理页互相污染。结论：**新建 `test_plans/test_plan_items/test_cycles/test_cycle_items`**，通过 `requirement_id` 关联需求、通过 `source/source_id` 关联批次；042 的回归计划保留，后续可在其详情页加「转为测试计划」的入口（不在本期）。

## R2. 轮次如何驱动执行

现有 `core.runner.run_batch_test_cases(case_ids, project_id, batch_id=..., environment_id=...)` 支持传入已创建的批次，且批次已有 `source/source_id`（031/033 已有 `api_scenario` 用法）。结论：

1. 轮次启动时先 `create_run_batch(..., source="test_cycle", source_id=cycle.id)`；
2. 后台任务调用 `run_batch_test_cases(case_ids, project_id, batch_id=batch.id, environment_id=cycle.environment_id, triggered_by=...)`；
3. 不修改执行器签名，不新增来源枚举表。

`source` 列长度 32，`test_cycle` 足够；`source_id` 已有索引。

## R3. 收尾回填放在哪

049 已把三个收尾入口统一到 `app/services/batch_finalize.py::finalize_batch()`（等批次结束 → 分类汇总 → 环境重试 → 通知）。轮次回填放在 `finalize_batch` 的**通知之后**：

- 只处理 `batch.source == "test_cycle"` 且 `source_id` 非空的批次；
- 若批次被自动重试，重试批次同样带 `source="test_cycle"`（`create_rerun_batch` 继承原批次 source/source_id），因此后到的重试批次会覆盖轮次统计——符合「以最新结果为准」；
- 轮次状态：批次 completed → cycle.completed；cancelled → cycle.cancelled；
- 计划项状态：遍历批次里的 `test_runs`，按 `case_id` 找 `test_plan_item`，passed/failed 直接写；没有运行的项保持原状态（跳过的病例不误伤）。

**幂等**：`finalize_batch` 可能被重复调用（通知重试/进程重启），回填只用 `set` 幂等赋值，不累加；重复执行结果一致。

## R4. 轮次状态机

```text
planned ──start──> running ──batch completed──> completed
   │                  │
   │                  └──batch cancelled──> cancelled
   └──cancel──> cancelled
```

- 同一计划同一时刻只允许一个 `running`；启动时行锁/状态检查，冲突返回 409。
- `planned` 可重复编辑用例选择；`running` 后不再改快照。
- 重跑失败：轮次保持 `running`，新建批次并把 `cycle.batch_id` 指向新批次；旧批次仍可查报告。

## R5. 计划项状态语义

| 值 | 来源 |
|---|---|
| `not_started` | 新建计划项默认 |
| `in_progress` | 人工标记（开始处理） |
| `passed` | 轮次回填，或人工标记 |
| `failed` | 轮次回填，或人工标记 |
| `blocked` | 人工标记（已知阻塞） |

轮次回填**覆盖** passed/failed/blocked（下一次轮次是新的证据）；人工 `in_progress` 也会被覆盖。这样避免「旧结论压住新结果」。计划进度只统计 `passed/failed/not_started/in_progress/blocked`，用例已删除项不计入分母。

## R6. 权限口径

复用 `app/auth.py::get_user_project_filter()`：

- admin → 所有项目；
- tester → `user.project_ids or []`。

新端点统一用 `ensure_project_access(user, project_id)`，越权 403；列表端点按 `project_ids` 过滤。计划项负责人必须是「项目可见用户」：admin 或 `project_ids` 包含该项目的用户；当前实现直接校验目标用户 `role=="admin" or project_id in (target.project_ids or [])`。

## R7. 评论与 @ 提及

- 表：`comments(project_id, entity_type, entity_id, user_id, body, mentions JSON, created_at, updated_at)`；v1 只开放 `entity_type="case"`。
- 解析：正则 `@([A-Za-z0-9_\u4e00-\u9fa5.-]{1,64})`，按 `username` 精确匹配（不区分大小写）；不存在则当普通文本。
- 通知：被提及者 + 用例创建者（若与提及者不同）各一条站内通知；不重复通知自己。
- 权限：读/写都要求项目可见；编辑/删除仅作者或 admin。
- 正文纯文本渲染（前端用文本节点，`white-space: pre-wrap`），长度 1–5000。

## R8. 人工评审

- 表：`case_reviews(project_id, case_id, requester_id, reviewer_id, status, note, comment, created_at, decided_at, updated_at)`。
- 状态机：`pending → approved | changes_requested | rejected`；`pending → cancelled`（发起人或 admin）。
- 唯一性：同一用例最多一条 `pending`；应用层检查（事务内 `SELECT ... FOR UPDATE` 在 PG 可用，sqlite 用普通查询 + 唯一索引兜底）。为兼容 sqlite，建普通索引 `(case_id, status)`，不建 partial unique。
- 约束：reviewer ≠ requester；reviewer 必须有项目权限；用例必须存在且属于同一项目。
- 通知：发起 → 评审人；结论 → 发起人。
- 历史：`GET /api/case-reviews?case_id=` 按时间倒序；用例详情显示最新一条。

## R9. 前端落点

- 路由/菜单：`frontend/src/routes.ts` 增 `plans`、`my-tasks`；详情页 `plans/:id` 为 ignore 路由。
- 页面映射沿用现有方式（`frontend/src/pages` 目录约定 + 路由 key），实现时确认组件注册位置。
- 计划详情用 Arco `Tabs`（用例 / 轮次）、`Table`、`Progress`、`Modal`；我的待办用 `Table` + 状态筛选。
- 用例评论/评审：在用例详情抽屉加「评论」「评审」两个 Tab，复用现有 Drawer；不新建独立详情页。
- 所有文案进 `frontend/src/locale/index.ts`（zh/en）。

## R10. 不做的事（本期）

- 组织/多租户/自定义 RBAC：需要改所有权限判定与数据隔离，单独立项。
- 邮件/IM/Webhook 通知：P0 渠道集成单独做；本期只发站内通知。
- 测试计划模板、跨项目计划、多人会签评审：留到后续。
- 计划与 042 回归计划的自动转换：先并存，后续加「转为测试计划」。
