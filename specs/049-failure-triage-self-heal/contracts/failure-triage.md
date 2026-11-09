# Contracts: 失败分类与自愈

所有端点沿用现有鉴权（`get_current_user`）与项目可见性规则；越权/不存在统一 404。

## 1. `GET /api/reports/batches/{batch_id}/failure-summary`

批次分类汇总。

**Response 200**

```json
{
  "batch_id": 665,
  "total": 12,
  "passed": 9,
  "failed": 3,
  "kinds": { "env": 2, "assertion": 1 },
  "real_defects": 1,
  "auto_retry": false,
  "retry_batch_id": 666,
  "retry_batch_status": "passed"
}
```

| 字段 | 说明 |
|---|---|
| `kinds` | 仅出现在失败运行中，值为 `0` 的类别省略 |
| `real_defects` | 非 `env` 失败的条数（含 `unknown`） |
| `auto_retry` | 本批次自身是否由自动重试创建 |
| `retry_batch_id` | 指向由本批次派生的自动重试批次；无则 `null` |

**错误**：404 批次不存在或无权访问。

## 2. `GET /api/reports/batches/{batch_id}`（扩展）

既有批次详情，`runs[]` 增加两个字段，并支持按类别过滤：

| 参数 | 类型 | 说明 |
|---|---|---|
| `failure_kind` | string（可选） | 仅返回该类别；`unknown` 同时匹配空值 |
| `dataset_group` | int（可选） | 既有参数，行为不变 |

`runs[]` 新增：

```json
{ "failure_kind": "env", "failure_reason": "502 Bad Gateway · upstream connect error" }
```

历史数据返回 `null`，不报错。

## 3. `POST /api/reports/batches/{batch_id}/rerun-failed`（扩展）

新增可选查询参数 `kinds`（逗号分隔，如 `kinds=env`），默认沿用旧行为（全部失败用例）。

**Response 200**

```json
{ "batch_id": 667, "total": 2, "parent_batch_id": 665, "status": "running", "kinds": ["env"] }
```

| 情况 | 状态码 | detail |
|---|---|---|
| 批次不存在/无权 | 404 | 批次不存在 |
| 批次仍在运行 | 409 | 批次仍在运行，请等结束后再重跑失败用例 |
| 指定类别下无失败用例 | 409 | 该批次没有指定类别的失败用例 |
| 同批次已有自动重试批次（仅内部自动调用） | 200 | 返回既有批次，幂等 |

## 4. 自动重试（内部契约，非 HTTP）

`app/services/batch_finalize.py::finalize_batch(batch_id, user_id)`：

1. 等待批次结束（沿用 `notify_batch_completed` 的轮询与超时）。
2. 汇总分类 → 写入 `run_batches.failure_summary`。
3. 若 `failure_auto_retry_enabled` 且存在可重试类别且批次未被取消：
   - 幂等检查：已存在 `parent_batch_id=本批次 AND auto_retry=true` 的子批次 → 跳过。
   - 取前 `failure_auto_retry_max_cases` 条可重试失败，创建子批次（`auto_retry=true`，命名「… · 环境重试」）。
4. 按分类决定通知级别并落库（`notifications`）。

## 5. 通知文案

| 场景 | type | message 模板 |
|---|---|---|
| 全部通过 | `success` | 通过 {passed}/{total}，全部通过 |
| 仅 `env` 且已创建重试批次 | `info` | 失败 {failed}（环境 {env}）· 已自动重试 {n} 条，批次 #{retry_id} |
| 仅 `env` 且开关关闭 | `info` | 失败 {failed}（环境 {env}）· 可开启自动重试 |
| 存在非 `env` 失败 | `error` | 失败 {failed}：真实缺陷 {real} / 环境 {env} / 定位 {locator} / 数据 {data} / 脚本 {script} / 未知 {unknown} |

> 敏感信息：`failure_reason` 与通知文案均复用现有 `mask_text()`（`core/ui_failure_evidence.py`），不落 cookie/token/密码。
