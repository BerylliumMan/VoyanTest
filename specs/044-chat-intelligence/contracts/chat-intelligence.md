# Contracts: 智能体对话能力增强

**Feature**: 044-chat-intelligence
**Date**: 2026-10-29

## C1. 记忆列表

`GET /api/chat/memory?project_id={id}`

**认证**: 必需；仅项目成员可访问。

**响应 200**:

```json
{
  "items": [
    {
      "name": "run-habit",
      "description": "用户偏好先跑冒烟用例",
      "type": "feedback",
      "written_at": "2026-10-29T16:00:00+08:00",
      "body_preview": "先执行 smoke 用例集，再执行全量。"
    }
  ],
  "total": 1
}
```

**错误**:
| 状态码 | 场景 |
|---|---|
| 401 | 未登录 |
| 403 | 非项目成员 |
| 404 | 项目不存在 |

**约束**: `body_preview` 最多 200 字符；`items` 按 `written_at` 倒序；最多返回 200 条。

## C2. 删除记忆

`DELETE /api/chat/memory/{name}?project_id={id}`

**响应 200**:

```json
{"deleted": true, "name": "run-habit"}
```

**错误**:
| 状态码 | 场景 |
|---|---|
| 401 | 未登录 |
| 403 | 非项目成员 |
| 404 | 记忆不存在 |

**行为**: 删除后立即从索引移除；进行中的对话下一轮不再召回该条。

## C3. 开关行为契约

每个开关关闭时，对应能力不产出任何可观察差异：

| 开关 | 关闭后行为 |
|---|---|
| `chat_compact_enabled=false` | 仅使用原有字符预算与 `compact_messages` 兜底，不执行 microcompact / auto compact |
| `chat_plan_mode_enabled=false` | `enter_plan_mode` / `exit_plan_mode` 不出现在工具清单；提示词不含 Plan 段 |
| `chat_subagent_enabled=false` | `agent` 工具不出现在工具清单；子代理不可用 |
| `chat_memory_enabled=false` | 不注入记忆索引、不召回；记忆 API 返回空列表（管理界面仍可访问已存条目） |

**验证方式**: `tests/unit/chat/test_chat_flags.py` 对每个开关断言工具清单与提示词内容。

## C4. 工具反馈契约

所有工具结果进入模型上下文时，必须满足下列之一：

1. **成功**: JSON 序列化 `ToolResult.to_dict()`，可附带业务字段。
2. **截断**: 在成功 JSON 后追加恢复指引（含"翻页"或"收窄"字样）。
3. **失败**: `<tool_use_error>\n工具 {name} 执行失败：{message}\n修复建议：{hint}\n</tool_use_error>`。
4. **参数校验失败**: `<tool_use_error>` 内逐条列出问题，每条一行。

**约束**: 不得出现无结构的裸错误字符串；不得静默丢弃结果。

## C5. system-reminder 契约

- 注入格式固定为 `<system-reminder>\n{content}\n</system-reminder>`。
- 内容中出现嵌套 `<system-reminder>` 时转义为 `&lt;system-reminder>`。
- 提醒只能追加到工具结果消息尾部，不得插入 assistant 消息，避免破坏 tool_call 配对。
- 同一提醒在单个对话轮次内最多注入一次。
