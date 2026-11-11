# API Contract: 测试管理闭环

所有端点前缀 `/api`，复用 FastAPI 依赖 `get_current_user` / `get_async_db`；错误统一 `{"detail": "..."}`。权限：admin 全量；tester 仅 `user.project_ids`。

## 1. 测试计划

### GET /api/plans
查询参数：`project_id`（必填）、`status`（可选）、`include_archived`（bool，默认 false）。
响应：
```json
{
  "items": [{
    "id": 1, "project_id": 47, "name": "支付回归", "description": "",
    "requirement_id": 3, "owner_id": 5, "status": "active",
    "start_date": "2026-11-11", "due_date": "2026-11-30",
    "stats": {"total": 3, "passed": 1, "failed": 1, "not_started": 1,
              "in_progress": 0, "blocked": 0, "deleted": 0,
              "progress": 33.3},
    "created_at": "2026-11-11T03:00:00Z", "updated_at": "..."
  }]
}
```
错误：403 无项目权限。

### POST /api/plans
```json
{"project_id": 47, "name": "支付回归", "description": "可选",
 "requirement_id": null, "owner_id": 5,
 "start_date": "2026-11-11", "due_date": "2026-11-30"}
```
201 → 计划对象（含 stats）。

### GET /api/plans/{plan_id}
200 → 计划对象 + `stats` + `cycle_count` + `last_cycle`（id/name/status/passed/failed）。

### PATCH /api/plans/{plan_id}
任意字段：`name/description/requirement_id/owner_id/status/start_date/due_date`。
200 → 更新后的对象。归档走 `status="archived"`；非法状态 422。

### DELETE /api/plans/{plan_id}
仅允许删除**没有轮次**的计划；有轮次返回 409「请归档而不是删除」。200 `{"ok": true}`。

### GET /api/plans/{plan_id}/items
查询：`status`、`assignee_id`、`module_id`、`keyword`（可选）。
```json
{"items": [{"id": 1, "case_id": 3, "case_name": "下单成功", "case_kind": "ui",
  "module_id": 2, "assignee_id": 5, "assignee_name": "bob",
  "priority": "high", "due_date": "2026-11-20", "status": "not_started",
  "note": "", "sort_order": 0, "case_exists": true}]}
```

### POST /api/plans/{plan_id}/items
```json
{"case_ids": [3, 4, 5]}
```
200 `{"added": 2, "skipped": 1, "items": [...]}`；重复用例跳过；不存在的用例计入 skipped。
错误：400 计划已归档；403。

### PATCH /api/plans/{plan_id}/items/{item_id}
```json
{"assignee_id": 5, "due_date": "2026-11-20", "priority": "high",
 "status": "in_progress", "note": "...", "sort_order": 1}
```
200 → item。负责人校验（非项目成员 400）。指派变化时发通知。

### DELETE /api/plans/{plan_id}/items/{item_id}
200 `{"ok": true}`。

## 2. 轮次

### GET /api/plans/{plan_id}/cycles
```json
{"items": [{"id": 7, "name": "第 1 轮", "status": "completed",
  "batch_id": 670, "case_count": 3, "skipped_count": 0,
  "passed": 2, "failed": 1, "started_at": "...", "finished_at": "..."}]}
```

### POST /api/plans/{plan_id}/cycles
```json
{"name": "第 1 轮", "environment_id": 2, "agent_id": null,
 "case_ids": [3, 4, 5]}
```
`case_ids` 省略 = 全部有效计划项。轮次快照固化。
错误：400 计划归档；400 计划没有有效用例；400 case_ids 不属于该计划。
201 → `{id, name, status: "planned", case_count, skipped_count}`。

### GET /api/cycles/{cycle_id}
```json
{"id": 7, "plan_id": 1, "name": "第 1 轮", "status": "completed",
 "environment_id": 2, "batch_id": 670, "case_count": 3, "skipped_count": 0,
 "passed": 2, "failed": 1,
 "items": [{"case_id": 3, "case_name": "下单成功", "assignee_id": 5,
            "run_status": "passed", "failure_kind": null, "run_id": 88}],
 "batches": [{"id": 670, "status": "completed", "passed": 2, "failed": 1}]}
```

### POST /api/cycles/{cycle_id}/start
无请求体。200 `{"batch_id": 670, "status": "running"}`。
错误：409 已有 running 轮次 / 轮次已在执行；400 没有有效用例；400 计划已归档。

### POST /api/cycles/{cycle_id}/cancel
仅 planned/running；running 时同时取消批次（复用 049 的停止路径，若不可用则标记 cancelled 并等待批次结束）。
200 `{"status": "cancelled"}`。

### POST /api/cycles/{cycle_id}/retry-failed
按最新批次中 `status="failed"` 的 case_id 新建批次；无失败返回 400。
200 `{"batch_id": 671, "case_count": 1}`。

## 3. 我的待办

### GET /api/my/tasks
查询：`filter=todo|overdue|done`（默认 todo）、`project_id`（可选）、`page/size`（可选）。
```json
{"total": 2, "items": [{
  "plan_item_id": 3, "plan_id": 1, "plan_name": "支付回归",
  "project_id": 47, "project_name": "商城", "case_id": 5,
  "case_name": "退款", "status": "not_started", "priority": "high",
  "due_date": "2026-11-10", "overdue": true,
  "assignee_id": 5, "assignee_name": "bob"}]}
```
`todo` = 状态非 passed；`overdue` = due_date < 今天且状态非 passed；`done` = passed。

## 4. 评论

### GET /api/comments?entity_type=case&entity_id=3
```json
{"items": [{"id": 1, "body": "@bob 这里断言太宽", "user_id": 5,
  "user_name": "alice", "mentions": [6], "can_edit": true,
  "created_at": "...", "updated_at": "..."}]}
```

### POST /api/comments
```json
{"entity_type": "case", "entity_id": 3, "body": "文本", "mentions": [6]}
```
`mentions` 可省略，服务端从 `@name` 解析并合并。201 → 评论对象。
错误：404 用例不存在；403 无项目权限；422 空/超长。

### PATCH /api/comments/{comment_id}
`{"body": "..."}`；仅作者/admin；200 → 更新对象；403/404。

### DELETE /api/comments/{comment_id}
仅作者/admin；200 `{"ok": true}`。

## 5. 人工评审

### POST /api/case-reviews
```json
{"case_id": 3, "reviewer_id": 6, "note": "请确认断言"}
```
201 → `{id, status: "pending", reviewer_id, requester_id, created_at}`。
错误：400 reviewer == requester；400 reviewer 无项目权限；404 用例不存在；409 已有 pending。

### GET /api/case-reviews
查询：`status`、`mine=pending`（当前用户为评审人）、`case_id`、`project_id`。
```json
{"items": [{"id": 1, "case_id": 3, "case_name": "下单成功",
  "requester_id": 5, "requester_name": "alice",
  "reviewer_id": 6, "reviewer_name": "bob",
  "status": "pending", "note": "", "comment": null,
  "created_at": "...", "decided_at": null}]}
```

### POST /api/case-reviews/{review_id}/decision
```json
{"status": "approved", "comment": "OK"}
```
`status ∈ approved|changes_requested|rejected`。仅 reviewer；不是 pending 返回 409。
200 → 更新对象；通知发起人。

### POST /api/case-reviews/{review_id}/cancel
仅 requester/admin；pending 才可取消；200。

### GET /api/cases/{case_id}/reviews
历史，按 `created_at` 倒序；含 reviewer/requester 名字。

## 6. 错误码约定

| 码 | 场景 |
|---|---|
| 400 | 参数/状态不允许（归档计划启动、空用例、负责人非法） |
| 403 | 无项目权限、非作者编辑评论、非评审人决策 |
| 404 | 计划/轮次/用例/评论/评审不存在 |
| 409 | 轮次正在执行、已有 pending 评审、删除有轮次的计划 |
| 422 | 枚举值非法、评论空/超长 |
