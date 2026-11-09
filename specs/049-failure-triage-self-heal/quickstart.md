# Quickstart: 失败分类与自愈

## 前置

- 服务端已部署本特性镜像；开关默认开启：`failure_triage_enabled=true`、`failure_auto_retry_enabled=true`。
- 准备三类失败样本：
  1. **环境**：指向一个关闭的端口（如 `http://127.0.0.1:9/health`）→ 连接失败。
  2. **断言**：平台内 `GET /api/api-test/health`，在用例中把断言改成「响应包含 `not-exist-field`」。
  3. **鉴权**：调用需要登录的接口但不带凭证 → 401。

## 1. 分类可见

1. 把三条用例放进同一批次执行。
2. 打开报告详情：
   - 连接失败的行显示 **环境**，原因含 `ConnectError`/`connection refused`；
   - 断言失败的行显示 **断言**；
   - 401 的行显示 **鉴权**。
3. 顶部摘要显示 `环境 1 / 断言 1 / 鉴权 1`，「真实缺陷 2」。

## 2. 自动重试只碰环境

1. 等待批次结束（默认轮询），观察是否自动出现新批次「… · 环境重试」，且只含 1 条环境用例。
2. 原批次详情出现「环境重试」徽标，可跳到重试批次。
3. 断言与鉴权用例**没有**进入重试批次。
4. 重试批次若仍失败，不会再生出新批次（最多 1 轮）。

## 3. 通知降噪

| 场景 | 预期通知 |
|---|---|
| 只有环境失败、重试已创建 | `info`，文案含「已自动重试」 |
| 只有环境失败、开关关闭 | `info`，文案提示可开启 |
| 自动重试批次仍然只有环境失败 | `error`，文案含「环境重试后仍失败」 |
| 含断言/鉴权失败 | `error`，文案含「真实缺陷 N」 |
| 全部通过 | `success` |

## 4. 关闭开关回归

```bash
FAILURE_TRIAGE_ENABLED=false FAILURE_AUTO_RETRY_ENABLED=false
```

- 批次收尾行为与 048 一致：不建重试批次、通知仍按老规则发 `error`。
- 报告页类别列可显示「未知」（不报错）。

## 已知限制

- 历史批次（上线前）没有类别，显示「未知」，不参与自动重试。
- 分类基于错误文本与状态码，若上游把错误包装成自由文本，可能落到 `unknown`；`unknown` 视为真实缺陷，不自动重试。
- 客户端（Agent）执行同样走收尾分类，但需要与服务端同版本。
- 自动重试幂等由部分唯一索引 `ux_run_batches_auto_retry_parent` 兜底；若线上库在加索引前已产生重复自动重试批次，索引创建会告警，需要先手工清理重复行。
- 无状态码、且错误文本同时含 `schema` 的失败仍可能判为 `assertion`（research R2 的文本兜底），属于已知边界。

## 本地验证命令

```bash
venv/bin/python -m pytest \
  tests/unit/test_failure_triage.py \
  tests/unit/test_failure_retry.py \
  tests/unit/test_batch_finalize.py -q

venv/bin/python -m pytest tests/contract/test_failure_summary_api.py -q

cd frontend && npx tsc --noEmit && npm run build
```

## 真机验证记录（2026-11-09，voyantest:049）

| 场景 | 用例 | 批次 | 结果 |
|---|---|---|---|
| 环境失败 | #752 连接 127.0.0.1:9 | #666 | `failure_kind=env`，自动重试批次 **#667** 只含该用例 |
| 断言失败 | #753 期望 500 实际 200 | #666 | `failure_kind=assertion`，不进入重试 |
| 鉴权失败 | #754 受保护接口 401 | #666 | `failure_kind=auth`（响应状态优先于断言），不进入重试 |
| 环境抖动后恢复 | #755 首次 503、重试 200 | #668 → **#669** | 重试批次 **passed 1/1**，原批次通知降级为 `info` |

通知验证：

| 批次 | 类型 | 文案 |
|---|---|---|
| #666（混合失败） | `error` | 失败 3：真实缺陷 1 / 鉴权 1 / 环境 1 |
| #667（重试仍失败） | `error` | 环境重试后仍失败 1（环境 1） |
| #668（仅环境 + 已重试） | `info` | 失败 1（环境 1）· 已自动重试 1 条，批次 #669 |
| #669（重试通过） | `success` | 通过 1/1，全部通过 |

幂等性：批次 #666 只派生一个重试批次 #667；#667 自身不再派生（`retry_batch_id=null`）。
