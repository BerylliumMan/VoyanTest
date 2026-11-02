# API Contract: 046 仓库同步与变更计划

所有端点挂在 `/api/api-test` 下，需要登录；项目权限按现有 `get_user_project_filter` 校验。

## 仓库绑定

### GET `/repos/{project_id}`

```json
{ "bound": true, "binding": {
  "id": 2, "project_id": 47,
  "repo_url": "https://git.example.com/a/b.git",
  "branch": "main", "auth_type": "https_token",
  "has_secret": true, "auth_secret": "******",
  "sync_mode": "scheduled", "sync_interval_minutes": 30,
  "extractor": "auto", "openapi_url": null, "source_root": null,
  "confirm_window_minutes": null, "full_sync_every": 10,
  "last_sync_at": "...", "last_commit": "...", "last_error": null
}}
```

未绑定：`{ "bound": false, "binding": null }`。

### POST `/repos`

`created` 为 false 表示更新了既有绑定（项目唯一）。

请求（`project_id` 必填）：

```json
{
  "project_id": 47, "repo_url": "...", "branch": "main",
  "auth_type": "https_token", "auth_secret": "token 或 user:token",
  "sync_mode": "scheduled", "sync_interval_minutes": 30,
  "extractor": "auto", "openapi_url": null, "source_root": null,
  "confirm_window_minutes": 60, "full_sync_every": 10
}
```

- `auth_secret` 传 `"******"` 或留空 = 不修改
- URL 内的 `user:pass` 会被拆出加密，返回的 `repo_url` 已打码

### PUT `/repos/{project_id}`

部分更新；`source_root` / `openapi_url` 传空串或 null 表示清空。

### DELETE `/repos/{project_id}`

解绑；返回 `{"deleted": project_id}`。

### POST `/repos/{project_id}/test`

只读连通性测试：

```json
{ "ok": true, "head": "sha", "branches": ["main"], "branch_exists": true }
```

### POST `/repos/{project_id}/sync?wait=false&check=false`

- `wait=true`：同步等待完成
- `check=true`：同步完成后立即计算变更并生成计划

返回 `{"status": "started", "check": false}` 或 `{"status": "done"}`。

### POST `/repos/{project_id}/sync/cancel`

协作式取消：

```json
{ "requested": true, "memory_hit": true, "project_id": 47 }
```

### GET `/repos/{project_id}/syncs?limit=20&status=running`

```json
{ "items": [{
  "id": 12, "trigger": "manual", "status": "success",
  "started_at": "...", "finished_at": "...",
  "base_commit": "...", "head_commit": "...",
  "added": 1, "updated": 0, "removed": 0, "error": null,
  "details": { "incremental": true, "changed_files": ["src/A.java"], "warnings": [] }
}]}
```

## 变更计划

### GET `/change-plans?project_id=47&status=pending&limit=50`

```json
{ "items": [{
  "id": 3, "plan_id": 7, "status": "pending", "decision": null,
  "from_commit": "...", "to_commit": "...",
  "case_count": 1,
  "affected": [{"definition_id": 43, "method": "GET", "path": "/api/users/{id}",
                "case_ids": [745], "changed_files": ["src/...java"]}],
  "deadline_at": "...", "seconds_left": 3300,
  "retry_count": 0, "retry_at": null, "batch_id": null
}]}
```

### POST `/change-plans/{id}/confirm`

立即认领并执行；返回 `{"id": 3, "status": "running", "decision": "confirmed"}`。
状态非 `pending` → 409。

### POST `/change-plans/{id}/cancel`

请求体 `{"reason": "..."}` 可选；返回 `{"id": 3, "status": "cancelled"}`。
状态非 `pending` → 409。

## 接口定义

### PUT `/definitions/{id}`

新增支持 `manual_override`（布尔）：

```json
{ "manual_override": true }
```

标记后同步不覆盖字段、也不被下线；传 `false` 恢复自动同步。

`GET /definitions` 与详情返回 `source_file / source_line / source_symbol / extractor / confidence / confidence_reason / status / manual_override`。
