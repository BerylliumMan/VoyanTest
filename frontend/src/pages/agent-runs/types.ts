/** Agent Run 状态 */
export type AgentRunStatus =
  | 'pending'
  | 'running'
  | 'paused'
  | 'completed'
  | 'failed'
  | 'cancelled';

/** Agent Run 列表项 */
export interface AgentRun {
  id: number;
  agent_definition_id: number;
  agent_definition_name: string;
  status: AgentRunStatus;
  goal?: Record<string, unknown> | string;
  turns_used: number;
  started_at: string | null;
  completed_at: string | null;
  duration_ms: number | null;
  error?: string;
  created_at?: string;
}

/** OTA Turn 中的工具调用 */
export interface ToolCall {
  id: string;
  name: string;
  arguments: Record<string, unknown> | null;
  result: string | null;
  status: string;
  error: string | null;
}

/** 单轮对话（OTA Turn） */
export interface OTATurn {
  id: string;
  turn_number: number;
  role: 'user' | 'assistant' | 'tool' | 'system';
  content: string;
  timestamp: string;
  tool_calls: ToolCall[];
}

/** Agent Run 详情（含 turns 列表） */
export interface AgentRunDetail extends AgentRun {
  turns: OTATurn[];
  error_message?: string | null;
  agent_definition_id: number;
}

/** WebSocket 推送消息 */
export type WsAgentRunMessage =
  | { type: 'turn_start'; turn: OTATurn }
  | { type: 'turn_update'; turn_id: string; content_delta?: string }
  | { type: 'status_change'; status: AgentRunStatus; timestamp: string };
