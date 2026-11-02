# Data Model: 046 Java 接口自动提取与变更触发

## code_repo_bindings（项目 ↔ 仓库，1:1）

| 字段 | 类型 | 说明 |
|---|---|---|
| id | serial PK | |
| project_id | int FK projects，唯一 | 一个项目一个绑定 |
| repo_url | varchar(500) | HTTPS / SSH（userinfo 已拆分） |
| branch | varchar(200) | 空 = 远端默认 |
| auth_type | varchar(16) | `https_token` / `ssh_key` |
| auth_secret | text | 加密后的 token 或私钥 |
| sync_mode | varchar(16) | `manual` / `scheduled` |
| sync_interval_minutes | int | 5–1440 |
| extractor | varchar(32) | `auto` / `openapi` / `spring_source` |
| openapi_url | varchar(500) | 可选 |
| source_root | varchar(300) | 可选，多模块 |
| confirm_window_minutes | int NULL | 项目级确认窗口；NULL=继承全局 |
| full_sync_every | int | 每 N 次增量后全量校准，默认 10 |
| last_sync_at / last_commit / last_error | | 同步状态 |

## api_sync_records（同步记录）

| 字段 | 类型 | 说明 |
|---|---|---|
| id / project_id / trigger | | trigger：`manual` / `schedule` |
| started_at / finished_at | timestamptz | |
| base_commit / head_commit | varchar(64) | |
| added / updated / removed | int | 合并统计 |
| status | varchar(16) | `running` / `success` / `no_change` / `failed` / `cancelled` |
| error | text | |
| details | jsonb | `extractors/incremental/full_sync/incremental_count/changed_files/warnings/errors/tech/stats` |
| cancel_requested | bool | 取消标志 |
| cancelled_at | timestamptz | |

## api_change_triggers（变更计划）

| 字段 | 类型 | 说明 |
|---|---|---|
| id / project_id | | |
| from_commit / to_commit | varchar(64) | |
| plan_id | FK quality_regression_plans | 复用回归计划 |
| status | varchar(20) | `pending` / `running` / `done` / `failed` / `cancelled` / `expired_skipped` |
| deadline_at | timestamptz | 确认窗口截止 |
| claimed_at / retry_at / retry_count | | 原子认领与退避 |
| decided_at / decided_by / decision / reason | | 决策留痕 |
| case_count / batch_id | | 执行结果 |
| affected | jsonb | `[{definition_id, method, path, case_ids, changed_files}]` |

## api_definitions 扩列

| 字段 | 类型 | 说明 |
|---|---|---|
| source_file | varchar(500) | 仓库相对路径 |
| source_line | int | |
| source_symbol | varchar(300) | 类名.方法名 |
| extractor | varchar(32) | `openapi` / `spring_source` / `manual` |
| confidence | varchar(16) | high / medium / low |
| confidence_reason | varchar(300) | 低可信原因 |
| status | varchar(16) | active / offline |
| manual_override | bool | true 时同步不覆盖、不下线 |

## 关系

```
projects 1──1 code_repo_bindings
projects 1──N api_sync_records
projects 1──N api_change_triggers N──1 quality_regression_plans
projects 1──N api_definitions N──N test_cases (api_definition_id)
```

## 状态机

```
pending ──确认──> running ──> done
   │                 │
   │                 └──失败──> pending（5/15 分钟退避，最多 2 次）──> failed
   ├──取消──> cancelled
   └──超窗超宽限──> expired_skipped
```
