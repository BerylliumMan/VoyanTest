# Data Model: 测试管理闭环

新增 6 张表，均由 `Base.metadata.create_all` 创建（启动时执行），并在 `app/main.py` 的幂等 DDL 清单中补 `CREATE INDEX IF NOT EXISTS`（用于既有库升级索引）。不修改既有表结构。

## 枚举

### PlanStatus
`draft`（草稿，可编辑，不允许启动轮次）→ `active`（进行中，可启动轮次）→ `completed`（全部项通过后自动置位，可手动改回）→ `archived`（归档，只读；历史轮次可见）。

### PlanItemStatus
`not_started` / `in_progress` / `passed` / `failed` / `blocked`。

### CycleStatus
`planned` / `running` / `completed` / `cancelled`。

### ReviewStatus
`pending` / `approved` / `changes_requested` / `rejected` / `cancelled`。

## 表：test_plans

| 列 | 类型 | 默认 | 说明 |
|---|---|---|---|
| id | Integer PK | | |
| project_id | Integer FK projects.id | 必填 | 索引 |
| name | String(200) | 必填 | | |
| description | Text | NULL | | |
| requirement_id | Integer FK quality_requirements.id | NULL | 可选需求关联 |
| owner_id | Integer FK users.id | NULL | 计划负责人 |
| status | String(16) | `draft` | 索引 |
| start_date | Date | NULL | |
| due_date | Date | NULL | |
| created_by | Integer FK users.id | NULL | |
| created_at / updated_at | DateTime(timezone) | tz_now | |

## 表：test_plan_items

| 列 | 类型 | 默认 | 说明 |
|---|---|---|---|
| id | Integer PK | | |
| plan_id | Integer FK test_plans.id ON DELETE CASCADE | 必填 | 索引 |
| case_id | Integer FK test_cases.id ON DELETE SET NULL | NULL | 用例删除后置空，保留快照 |
| case_name | String(255) | 必填 | 加入时的名称快照 |
| case_kind | String(16) | `functional` | 快照，用于筛选/展示 |
| assignee_id | Integer FK users.id | NULL | 索引 |
| priority | String(16) | `medium` | low/medium/high/critical |
| due_date | Date | NULL | 索引 |
| status | String(16) | `not_started` | 索引 |
| note | Text | NULL | |
| sort_order | Integer | 0 | |
| created_at / updated_at | DateTime(timezone) | tz_now | |

唯一约束 `uq_test_plan_items_plan_case (plan_id, case_id)`（case_id 为 NULL 时不冲突）。

## 表：test_cycles

| 列 | 类型 | 默认 | 说明 |
|---|---|---|---|
| id | Integer PK | | |
| plan_id | Integer FK test_plans.id ON DELETE CASCADE | 必填 | 索引 |
| project_id | Integer FK projects.id | 必填 | 索引（列表过滤） |
| name | String(200) | 必填 | | |
| environment_id | Integer FK environments.id | NULL | 启动时传给执行器 |
| agent_id | Integer FK agent_definitions.id | NULL | 预留（v1 执行走项目默认） |
| status | String(16) | `planned` | 索引 |
| batch_id | Integer FK run_batches.id | NULL | 最新批次 |
| case_count | Integer | 0 | 快照用例数 |
| skipped_count | Integer | 0 | 创建时因用例删除而跳过 |
| created_by | Integer FK users.id | NULL | |
| started_at / finished_at | DateTime(timezone) | NULL | |
| created_at / updated_at | DateTime(timezone) | tz_now | |

## 表：test_cycle_items

| 列 | 类型 | 默认 | 说明 |
|---|---|---|---|
| id | Integer PK | | |
| cycle_id | Integer FK test_cycles.id ON DELETE CASCADE | 必填 | 索引 |
| plan_item_id | Integer FK test_plan_items.id ON DELETE SET NULL | NULL | |
| case_id | Integer FK test_cases.id ON DELETE SET NULL | NULL | |
| case_name | String(255) | 必填 | 快照 |
| case_kind | String(16) | `functional` | 快照 |
| assignee_id | Integer FK users.id | NULL | 创建时从计划项复制 |
| sort_order | Integer | 0 | |

唯一约束 `uq_test_cycle_items_cycle_case (cycle_id, case_id)`（case_id 为 NULL 时不冲突）。

## 表：comments

| 列 | 类型 | 默认 | 说明 |
|---|---|---|---|
| id | Integer PK | | |
| project_id | Integer FK projects.id | 必填 | 索引 |
| entity_type | String(16) | `case` | v1 只用 case |
| entity_id | Integer | 必填 | 索引 |
| user_id | Integer FK users.id | 必填 | |
| body | Text | 必填 | 1–5000 字 |
| mentions | JSON | NULL | 被提及用户 id 列表 |
| created_at / updated_at | DateTime(timezone) | tz_now | 有编辑时更新 updated_at |

联合索引 `ix_comments_entity (entity_type, entity_id, created_at)`。

## 表：case_reviews

| 列 | 类型 | 默认 | 说明 |
|---|---|---|---|
| id | Integer PK | | |
| project_id | Integer FK projects.id | 必填 | 索引 |
| case_id | Integer FK test_cases.id ON DELETE CASCADE | 必填 | 索引 |
| requester_id | Integer FK users.id | 必填 | |
| reviewer_id | Integer FK users.id | 必填 | 索引 |
| status | String(16) | `pending` | 索引 |
| note | Text | NULL | 发起说明 |
| comment | Text | NULL | 评审结论 |
| created_at | DateTime(timezone) | tz_now | |
| decided_at | DateTime(timezone) | NULL | |
| updated_at | DateTime(timezone) | tz_now | |

索引 `ix_case_reviews_case_status (case_id, status)`；应用层保证同一用例最多一条 pending（sqlite 兼容，不建 partial unique）。

## 幂等 DDL（app/main.py）

```sql
-- 新表由 create_all 建立；以下只补既有库可能缺失的索引
CREATE INDEX IF NOT EXISTS ix_test_plans_project ON test_plans (project_id, status);
CREATE INDEX IF NOT EXISTS ix_test_plan_items_plan_status ON test_plan_items (plan_id, status);
CREATE INDEX IF NOT EXISTS ix_test_plan_items_assignee ON test_plan_items (assignee_id, due_date);
CREATE INDEX IF NOT EXISTS ix_test_cycles_plan ON test_cycles (plan_id, created_at);
CREATE INDEX IF NOT EXISTS ix_test_cycle_items_cycle ON test_cycle_items (cycle_id, sort_order);
CREATE INDEX IF NOT EXISTS ix_comments_entity ON comments (entity_type, entity_id, created_at);
CREATE INDEX IF NOT EXISTS ix_case_reviews_reviewer_status ON case_reviews (reviewer_id, status);
CREATE INDEX IF NOT EXISTS ix_case_reviews_case_status ON case_reviews (case_id, status);
```

## 状态流转

```text
TestPlan:      draft → active → completed
                  ↘ archived（任意状态可归档；archived 只读，可恢复 active）

TestPlanItem:  not_started → in_progress → passed | failed | blocked
               轮次回填会覆盖为 passed/failed（blocked/in_progress 亦然）

TestCycle:     planned → running → completed | cancelled
               planned → cancelled
               running --retry--> running

CaseReview:    pending → approved | changes_requested | rejected
               pending → cancelled
```

## 与既有表的关系

- `test_plan_items.case_id → test_cases.id`：SET NULL 保留快照；轮次创建时过滤 `case_id IS NULL` 并计入 `skipped_count`。
- `test_cycles.batch_id → run_batches.id`：弱关联；批次本身通过 `source='test_cycle' / source_id` 反查轮次，收尾回填不依赖外键。
- 通知：复用 `notifications` 表，`title/message/type`，不新增渠道字段。
