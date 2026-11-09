# Research: 失败分类与自愈

## R1. 失败信息从哪来

| 来源 | 内容 | 用途 |
|---|---|---|
| `core/api_runner/runner.py::ApiRunResult` | `status/error/steps[]`，步骤含 `assertions[]`（`passed/error`）、HTTP `status`、`error` | 接口失败的分类主依据 |
| 报告 JSON（`reports/*.json`） | `steps[]`，UI 与接口共用 | 执行期之外的二次分类、报告展示 |
| `run_logs` | 执行器异常时写入的 `CRITICAL` 文本 | 无报告文件时兜底 |
| `test_runs.status` | `passed/failed/cancelled` | 只作为"是否失败"的判据，不参与分类 |

结论：分类函数接收 `{"status", "error", "steps", "logs"}` 这一份最小结构即可，不直接依赖 ORM。

## R2. 分类优先级（定稿，真机验证后修正）

判定顺序（命中即停）：

0. **真实响应状态优先**（`step.response.status`，多步用例取失败步骤）：
   - 401/403 → `auth`
   - ≥500 → `env`
   - 404/409/410/422 → `data`
1. 断言失败（仅认 `passed=false`）→ `assertion`
2. 错误文本特征：定位 → 鉴权 → 数据 → 环境 → 脚本
3. 兜底 → `unknown`

**为什么状态优先于断言**：真机验证时发现，把「期望 200、实际 401」判成 `assertion` 会让过期 token 变成"真实缺陷"发 error 通知，自动重试也永远不会触发。响应已经回来时，断言只是把"未授权/服务挂了"写成了失败文案，按状态归类才符合"只通知真实缺陷"的目标。同理 5xx 归 `env`（可重试），而不归 `assertion`。

**为什么只认失败断言**：048 生成的用例都带 `status_code`/`jsonschema` 断言；若把断言**名称**当证据，环境故障会被误判为真实缺陷（`test_env_failure_with_passed_assertions_stays_env` 锁定该回归）。

## R3. 重跑链路能否复用

现有 `POST /api/reports/batches/{batch_id}/rerun-failed` 已实现：收集失败 `case_id`（含 `dataset_group`）、建子批次、写 `parent_batch_id`、后台执行、完成后发通知。

结论：把其中"收集 + 建批次 + 调度"下沉为 `app/services/batch_rerun.py::create_rerun_batch(db, batch, *, case_ids=None, kinds=None, name_suffix=...)`，路由与自动重试共用。自动重试只多两步：按 `failure_kind` 过滤 + 幂等检查。

## R4. 自动重试触发点

批次收尾目前有三个入口，都直接调 `notify_batch_completed`：

- `core/runner/_api_execution.py`（接口用例/场景）
- `core/agent_bridge.py`（客户端执行）
- `app/routers/report_router.py`（仅失败重跑）

结论：新增 `app/services/batch_finalize.py::finalize_batch(batch_id, user_id)`，内部依次「等批次结束（沿用原轮询）→ 汇总分类 → 视情况创建重试批次 → 发通知」。三处改为调用它；`notify_batch_completed` 保留为兼容入口。

## R5. 为什么不用 LLM 分类

- 分类发生在**每个批次收尾**，调用模型会让确认延迟从毫秒级变成秒级，且失败时无兜底。
- 规则可解释、可单测、可回归；041 的 LLM 缺陷报告仍是"人工触发的深度分析"，两者分层不冲突。
- 留出扩展点：`failure_triage` 输出 `confidence`（high/low），后续可只对 `low` 调模型复核。

## R6. 历史数据

`failure_kind` 为空的历史运行视为 `unknown`，不回填、不重算；报告接口对空值返回 `null`，前端显示「未知」。避免上线时对存量批次做批量扫描。

## R7. 开关默认值

| 开关 | 默认 | 理由 |
|---|---|---|
| `failure_triage_enabled` | `True` | 分类只读不写业务状态，风险低 |
| `failure_auto_retry_enabled` | `True` | 目标就是减少人工；限流与一致性保障已具备 |
| `failure_auto_retry_kinds` | `["env"]` | 只碰环境抖动 |
| `failure_auto_retry_max_cases` | `20` | 防止故障时放大执行量 |
| `failure_notify_real_only` | `True` | 降噪是核心诉求；关闭即回到现状 |
