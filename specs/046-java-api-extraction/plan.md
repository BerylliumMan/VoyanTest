# Implementation Plan: Java 接口自动提取与变更触发

**Branch**: `046-java-api-extraction` | **Date**: 2026-10-30 | **Spec**: [spec.md](./spec.md)

## Summary

为项目绑定 Git 仓库，按技术栈自动提取 Java 接口定义（OpenAPI 优先、Spring 源码兜底）；代码变更后算出受影响接口与用例，生成待确认回归计划，1 小时内无人处理则自动执行。

## Technical Context

**Language/Version**: Python 3.11（后端）、TypeScript + React（前端）
**Primary Dependencies**: 新增 `dulwich`（纯 Python Git，HTTPS）、`paramiko`（SSH 拉取）、`tree-sitter` + `tree-sitter-java`（源码解析）；复用现有 FastAPI / SQLAlchemy async / TaskScheduler
**Storage**: PostgreSQL；新增 3 张表 + `api_definitions` 扩列
**Testing**: pytest（提取器纯函数单测 + 契约测试）
**Constraints**: 不依赖系统 git（容器内无 git）；凭据加密存储；手工修正不被同步覆盖

## 关键现状

| 事实 | 影响 |
|---|---|
| 容器内无 git、无 dulwich | 需新增依赖，用 dulwich/paramiko 拉取 |
| `api_definitions` 已有 method+path 唯一键、source、operation_id | 扩列即可承载来源与可信度 |
| 已有 `QualityRegressionPlan`（items JSON = case_ids + 理由） | 变更计划直接复用，不新建计划表 |
| 已有 `TaskScheduler` | 定时同步与 1 小时超时扫描挂在这里 |
| 已有 CI 令牌与批量执行 | 自动执行复用现有批次创建 |

## 数据模型

### 新表

**`code_repo_bindings`**（项目 ↔ 仓库）

| 字段 | 类型 | 说明 |
|---|---|---|
| id | int PK | |
| project_id | int FK | 唯一（一个项目一个绑定） |
| repo_url | varchar(500) | HTTPS 或 SSH 地址 |
| branch | varchar(200) | 默认 main |
| auth_type | varchar(16) | `https_token` / `ssh_key` |
| auth_secret | text | 加密后的 token 或私钥 |
| sync_mode | varchar(16) | `manual` / `scheduled` |
| sync_interval_minutes | int | 默认 30 |
| extractor | varchar(32) | `auto` / `openapi` / `spring_source` |
| openapi_url | varchar(500) | 可选，显式指定 |
| last_sync_at / last_commit / last_error | | 同步状态 |
| created_at / updated_at | | |

**`api_sync_records`**（同步记录）

id, project_id, started_at, finished_at, trigger(`manual`/`schedule`), base_commit, head_commit, added, updated, removed, error, details(JSON)

**`api_change_triggers`**（变更触发记录）

id, project_id, from_commit, to_commit, plan_id(FK quality_regression_plans), status(`pending`/`confirmed`/`cancelled`/`auto_ran`/`expired_failed`), deadline_at, decided_at, decided_by, reason, case_count, created_at

### `api_definitions` 扩列

| 字段 | 类型 | 说明 |
|---|---|---|
| source_file | varchar(500) | 相对仓库路径 |
| source_line | int | 行号 |
| source_symbol | varchar(300) | 类名.方法名 |
| extractor | varchar(32) | openapi / spring_source / manual |
| confidence | varchar(16) | high / medium / low |
| confidence_reason | varchar(300) | 低可信度原因（如常量无法解析） |
| status | varchar(16) | active / offline |
| manual_override | boolean | true 时同步不覆盖 |

迁移方式沿用 `app/main.py` 的 `_ddl` + `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`。

## 提取器设计

```
core/api_extract/
├── base.py          # ExtractedApi 数据类 + Extractor 协议
├── openapi.py       # OpenAPI/Swagger 文档 → ExtractedApi[]
├── spring_source.py # tree-sitter-java 解析注解 → ExtractedApi[]
├── detect.py        # 技术栈探测（pom/gradle 依赖 + 源码特征）
└── registry.py      # 策略 → 提取器映射
```

统一输出：

```python
@dataclass
class ExtractedApi:
    method: str
    path: str
    summary: str = ""
    operation_id: str | None = None
    request_schema: dict = field(default_factory=dict)
    source_file: str | None = None
    source_line: int | None = None
    source_symbol: str | None = None
    extractor: str = "openapi"
    confidence: str = "high"
    confidence_reason: str | None = None
```

Spring 解析要点：
1. 类级 `@RequestMapping` + 方法级 `@GetMapping/@PostMapping/@PutMapping/@DeleteMapping/@PatchMapping/@RequestMapping(method=…)`
2. 路径常量：`static final String` 字面量与同文件拼接
3. 无法解析 → `confidence=low` + 原始表达式
4. 忽略 `@Deprecated` / `@Hidden`

## 同步流程

```
拉取（dulwich/paramiko 到临时目录，浅克隆）
→ 记录 base_commit / head_commit
→ detect 技术栈
→ 执行提取器
→ 与现有 api_definitions 按 (method, path) 合并：
    新增 / 更新（跳过 manual_override）/ 未出现→offline
→ 写 api_sync_records
```

## 变更触发流程

```
定时（每 5 分钟）检查各绑定仓库的远端 HEAD
→ 与 last_commit 比较
→ 有差异：git diff --name-only 列出变更文件
→ 命中 api_definitions.source_file
→ definition → 关联用例（api_test_cases.definition_id）
→ 生成 QualityRegressionPlan（items 带理由）+ api_change_triggers(deadline=now+60min)
→ 每 30 秒扫描到期且 pending 的触发 → 原子认领 → 自动执行
→ 人工确认/取消 → 立即执行 / 标记取消
```

### 调度设计（借鉴 zcode Automation 的调度内核）

参考：`packages/desktop/src/scheduler/index.ts`（轮询+认领）、`automationRepo.ts:737`（claimDue）、
`docs/automation-session-hourly-interval.md`（相对锚点语义）、`docs/automation-create-limit.md`（上限事务检查）。

**状态机**

```
pending ──确认──> confirmed ──> running ──> done / failed
   │
   ├──取消──> cancelled
   └──超时（宽限内）──> running（auto_ran）
       └──超过宽限──> expired_skipped
```

**原子认领（避免多 worker/重启重复执行）**

```sql
UPDATE api_change_triggers
SET status = 'running', claimed_at = now()
WHERE id = :id AND status = 'pending' AND deadline_at <= now()
-- rowcount == 1 才执行；否则跳过
```

**僵尸回收**：每轮扫描 `status='running' AND claimed_at < now() - 5min` → 转 `failed`（原因：执行进程丢失），不重试。

**错过窗口（服务停机跨过 deadline）**
- 宽限 10 分钟内：正常补执行（`auto_ran`）
- 超过宽限：转 `expired_skipped` 并留痕，**不补跑**（一次性任务不推导新的执行承诺）

**失败重试**：自动执行失败最多重试 2 次，退避 5 / 15 分钟（`retry_at` 优先于 deadline 判断），仍失败转 `failed`，不无限重试。

**限流**：每个项目同时处于 `pending` 的计划 ≤ 5 条；创建时事务内检查，超限则把新变更合并进最新一条 pending 计划（不新建）。

**合并**：同一 `from_commit` 已存在 pending 计划时，把新变更的用例并入该计划并刷新 `deadline_at`（从首次创建起算，不重置）。

## API 契约

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/api-test/repos` | 绑定仓库（含凭据加密） |
| GET | `/api/api-test/repos/{project_id}` | 查看绑定（凭据打码） |
| PUT | `/api/api-test/repos/{project_id}` | 更新绑定 |
| POST | `/api/api-test/repos/{project_id}/test` | 连通性测试 |
| POST | `/api/api-test/repos/{project_id}/sync` | 手动同步 |
| GET | `/api/api-test/repos/{project_id}/syncs` | 同步记录 |
| GET | `/api/api-test/change-plans` | 待确认计划列表（含倒计时） |
| POST | `/api/api-test/change-plans/{id}/confirm` | 立即执行 |
| POST | `/api/api-test/change-plans/{id}/cancel` | 取消 |

## 任务拆分

| # | 任务 | 文件 |
|---|---|---|
| T1 | 依赖与配置：dulwich/paramiko/tree-sitter-java，`api_extract_*` 配置 | `requirements.txt`、`app/config.py` |
| T2 | 数据模型 + 迁移 | `app/models/api_extract.py`、`app/main.py` |
| T3 | `ExtractedApi` + 提取器协议 + registry | `core/api_extract/base.py`、`registry.py` |
| T4 | OpenAPI 提取器（含 Swagger2） | `core/api_extract/openapi.py` |
| T5 | Spring 源码提取器（tree-sitter-java） | `core/api_extract/spring_source.py` |
| T6 | 技术栈探测 | `core/api_extract/detect.py` |
| T7 | Git 拉取（HTTPS/SSH，浅克隆，超时） | `core/git_sync.py` |
| T8 | 同步服务（合并/下线/手工保护） | `app/services/api_sync.py` |
| T9 | 变更影响（diff → 接口 → 用例 → 计划） | `app/services/api_change_impact.py` |
| T10 | 1 小时窗口 + 定时扫描 + 自动执行 | `app/services/api_change_plans.py`、`app/scheduler.py` |
| T11 | API 路由 | `app/routers/api_test/repos.py`、`change_plans.py` |
| T12 | 前端：仓库绑定面板、变更计划列表（倒计时/确认/取消） | `frontend/src/pages/api-test/` |
| T13 | 测试与回归 | `tests/unit/test_api_extract_*.py`、`tests/contract/test_api_repo_sync.py` |

## 风险与对策

| 风险 | 对策 |
|---|---|
| SSH 拉取依赖 paramiko 版本/算法兼容 | 连通性测试接口提前暴露错误 |
| 大仓库首次克隆慢 | 浅克隆（depth=1）；超时可配置；不阻塞调度器 |
| 路径常量无法解析 | 低可信度标记 + 保留原文，不编造 |
| 变更触发误报 | 文件级映射允许多报；合并去重；1 小时窗口可人工取消 |
| 自动执行影响环境 | 复用现有批次与并发限制；失败记录不重试 |

## 验收

按 spec SC-001~SC-009；以「真实 Java 仓库各一份有/无 OpenAPI」的提取结果 + 一次变更触发闭环为准。

---

## 增量设计 v2（2026-11-02）

### 数据模型补充

| 表 | 新增字段 | 说明 |
|---|---|---|
| `code_repo_bindings` | `confirm_window_minutes INT NULL` | 项目级确认窗口；NULL=继承全局 |
| `code_repo_bindings` | `full_sync_every INT NOT NULL DEFAULT 10` | 每 N 次增量后做一次全量校准 |
| `api_sync_records` | `cancelled_at TIMESTAMPTZ NULL`、`cancel_requested BOOL` | 手动取消 |

### 人工接管（FR-019）

- `PUT /api/api-test/definitions/{id}` 的 patch 增加 `manual_override`（显式布尔）；
- 前端详情来源条旁增加开关「人工修正」，开启后调用该接口；
- 同步合并时 `manual_override=True` 已跳过更新与下线（既有逻辑）。

### 项目级窗口（FR-020）

- `create_change_plan` 读取绑定上的 `confirm_window_minutes`，回退到全局；
- 绑定面板增加输入项，保存时校验 5–1440。

### 审计（FR-021）

- 复用 `app.services.audit.log_audit`：`api_sync.manual/schedule`、`api_change_plan.confirm/cancel/auto_run`；
- 记录 `project_id/action/details{trigger_id,batch_id,case_count}`。

### 同步状态与中断（FR-022）

- 同步开始时先 `commit` 一条 `running` 记录，结束更新；
- `POST /repos/{project_id}/sync/cancel` 写入 `cancel_requested`；
- 提取/解析循环在每个文件、每个提取器之间检查取消标志，命中则中止并记录 `cancelled`；
- 由于合并幂等，已应用的接口不会重复。

### 增量提取（FR-023）

- 同步拿到 `base_commit`/`head_commit` 后计算 `changed_files`；
- 对 `spring_source`：只解析变更的 `.java` 文件；未变更文件的既有接口保持不动；
- 每积累 `full_sync_every` 次增量执行一次全量（计数存 `api_sync_records.details`）；
- 首次同步（无 base）一律全量。

### 大仓库（FR-024）

- `detect` 与 `spring_source` 共用排除规则：`target/build/out/node_modules/.git/.gradle/.idea/generated`；
- 新增配置 `api_extract_max_scan_files`（默认 20000），超限在 `details.warnings` 明确提示。

### 手动检查变更（FR-025）

- `POST /repos/{project_id}/sync?check=true`：同步完成后立即执行一次影响计算与计划创建；
- 前端「立即同步」增加「同步并检查变更」按钮。

### 体验（FR-026）

- 解绑按钮（DELETE 既有接口）；
- 计划行批次号可点击跳转 `/reports?batch_id=`；
- 计划列表增加状态筛选；同步记录增加状态/触发筛选；
- 计划创建与超时自动执行调用 `notify_*`（站内通知）。
