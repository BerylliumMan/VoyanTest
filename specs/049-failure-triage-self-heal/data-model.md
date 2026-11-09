# Data Model: 失败分类与自愈

不新增表，只加列；沿用启动期幂等 DDL（`app/main.py` 中已有的 `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` 列表）。

## 枚举：FailureKind

| 值 | 含义 | 典型信号 | 自动重试 |
|---|---|---|---|
| `env` | 环境/基础设施 | 连接失败、超时、5xx、DNS、服务不可用 | ✅ 默认 |
| `auth` | 鉴权 | 401/403、token 失效、凭证缺失 | ❌ |
| `data` | 数据 | 目标不存在、前置数据缺失、404/409 | ❌ |
| `assertion` | 断言不成立（真实缺陷） | 断言 `passed=false`、结构校验失败 | ❌ |
| `locator` | 元素定位失败 | 等待元素超时、自愈未命中、selector 失效 | ❌ |
| `script` | 用例/脚本错误 | 变量未定义、表达式异常、数据集缺失 | ❌ |
| `unknown` | 无法判定或历史数据 | 空值、无法匹配的错误文本 | ❌ |

## test_runs（app/models/batch.py::TestRun）

| 列 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `failure_kind` | `VARCHAR(16)` NULL | NULL | 仅 `status=failed` 时写入；通过/取消为 NULL |
| `failure_reason` | `TEXT` NULL | NULL | 首处失败的简短原因，截断 500 字符，敏感值已在上游打码 |

写入点（仅两处，避免旁路）：

1. `core/runner/_persistence.py::save_test_run_result()` —— UI/API 正常跑完的落库入口。
2. `core/runner/_persistence.py::mark_run_failed()` —— 执行器异常兜底。

## run_batches（app/models/batch.py::RunBatch）

| 列 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `auto_retry` | `BOOLEAN NOT NULL` | `false` | 本批次是否由自动重试创建；为真时不再触发自动重试 |
| `failure_summary` | `JSON` NULL | NULL | 收尾时缓存的分类计数 `{"env":2,"assertion":1,...}`，供列表/通知读取，避免逐行聚合 |

## 索引

```sql
CREATE INDEX IF NOT EXISTS ix_test_runs_batch_kind
    ON test_runs (batch_id, failure_kind);
```

支撑「按批次 + 类别筛选」与汇总查询；单批次行数有限，索引成本可忽略。

## 迁移 DDL（写入 app/main.py 的启动补列清单）

```sql
ALTER TABLE test_runs    ADD COLUMN IF NOT EXISTS failure_kind VARCHAR(16);
ALTER TABLE test_runs    ADD COLUMN IF NOT EXISTS failure_reason TEXT;
ALTER TABLE run_batches  ADD COLUMN IF NOT EXISTS auto_retry BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE run_batches  ADD COLUMN IF NOT EXISTS failure_summary JSON;
```

## 兼容与生命周期

- 历史运行 `failure_kind IS NULL` → 接口返回 `null`，前端渲染「未知」，不参与自动重试。
- 批次汇总在收尾时计算一次并写入 `failure_summary`；读取时若为 NULL 则现场聚合（兼容历史批次）。
- 重试批次的 `parent_batch_id` 指向原批次，`auto_retry=true`；原批次的 `failure_summary` 不因重试结果改写，重试结论只看重试批次。
