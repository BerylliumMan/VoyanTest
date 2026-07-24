import { useState, useEffect, useRef, useCallback } from 'react';
import { apiRequest } from '@/utils/apiRequest';
import { AgentRunDetail, OTATurn, WsAgentRunMessage, AgentRunStatus } from '../types';

export interface UseAgentRunState {
  run: AgentRunDetail | null;
  turns: OTATurn[];
  wsStatus: 'connecting' | 'connected' | 'disconnected';
  loading: boolean;
}

export function useAgentRun(runId: string): UseAgentRunState {
  const [run, setRun] = useState<AgentRunDetail | null>(null);
  const [turns, setTurns] = useState<OTATurn[]>([]);
  const [wsStatus, setWsStatus] = useState<'connecting' | 'connected' | 'disconnected'>('disconnected');
  const [loading, setLoading] = useState(true);

  const wsRef = useRef<WebSocket | null>(null);
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const turnsMapRef = useRef<Map<string, OTATurn>>(new Map());

  /** 根据 turn id 更新或追加 turn */
  const upsertTurn = useCallback((turn: OTATurn) => {
    setTurns((prev) => {
      const map = new Map(prev.map((t) => [t.id, t]));
      const existing = map.get(turn.id);
      // 保留已有的 tool_calls（WS 增量更新时可能不传完整 tool_calls）
      const toolCalls = turn.tool_calls?.length
        ? turn.tool_calls
        : existing?.tool_calls || [];
      const content = turn.content || existing?.content || '';
      map.set(turn.id, { ...turn, content, tool_calls: toolCalls });
      turnsMapRef.current = map;
      return Array.from(map.values()).sort((a, b) => a.turn_number - b.turn_number);
    });
  }, []);

  /** 更新指定 turn 的 content（增量追加） */
  const appendTurnContent = useCallback((turnId: string, delta: string) => {
    setTurns((prev) => {
      const map = new Map(prev.map((t) => [t.id, t]));
      const existing = map.get(turnId);
      if (existing) {
        map.set(turnId, { ...existing, content: (existing.content || '') + delta });
      }
      turnsMapRef.current = map;
      return Array.from(map.values()).sort((a, b) => a.turn_number - b.turn_number);
    });
  }, []);

  /** WebSocket 连接 */
  const connectWs = useCallback(() => {
    if (!runId) return;

    try {
      const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
      const host = window.location.host;
      const url = `${protocol}//${host}/ws/agent-runs/${runId}`;
      const ws = new WebSocket(url);

      ws.onopen = () => {
        setWsStatus('connected');
        wsRef.current = ws;
        // 清除重连计时器
        if (reconnectTimerRef.current) {
          clearTimeout(reconnectTimerRef.current);
          reconnectTimerRef.current = null;
        }
      };

      ws.onmessage = (event) => {
        try {
          const msg: WsAgentRunMessage = JSON.parse(event.data);
          switch (msg.type) {
            case 'turn_start':
              upsertTurn(msg.turn);
              break;
            case 'turn_update':
              if (msg.content_delta !== undefined) {
                appendTurnContent(msg.turn_id, msg.content_delta);
              }
              break;
            case 'status_change':
              setRun((prev) => (prev ? { ...prev, status: msg.status } : prev));
              break;
          }
        } catch {
          // 忽略解析错误
        }
      };

      ws.onclose = () => {
        setWsStatus('disconnected');
        wsRef.current = null;
        // 自动重连：3 秒后重试
        reconnectTimerRef.current = setTimeout(() => {
          connectWs();
        }, 3000);
      };

      ws.onerror = () => {
        ws.close();
      };
    } catch {
      setWsStatus('disconnected');
    }
  }, [runId, upsertTurn, appendTurnContent]);

  /** 挂载时：获取 run 详情 + 历史消息 */
  useEffect(() => {
    let cancelled = false;

    const fetchData = async () => {
      try {
        setLoading(true);
        const runData: AgentRunDetail = await apiRequest(`/api/agent-runs/${runId}`);
        if (cancelled) return;
        setRun(runData);

        // 获取历史消息（已完成/失败的 run）
        try {
          const messages: OTATurn[] = await apiRequest(`/api/agent-runs/${runId}/messages`);
          if (!cancelled) {
            setTurns(messages.sort((a, b) => a.turn_number - b.turn_number));
            messages.forEach((m) => turnsMapRef.current.set(m.id, m));
          }
        } catch {
          // 消息接口可能不可用，忽略
        }
      } catch {
        // 获取失败
      } finally {
        if (!cancelled) setLoading(false);
      }
    };

    fetchData();

    return () => {
      cancelled = true;
    };
  }, [runId]);

  /** 当 run 状态为 running 时连接 WS */
  useEffect(() => {
    if (run?.status === 'running') {
      connectWs();
    }

    return () => {
      if (wsRef.current) {
        wsRef.current.close();
        wsRef.current = null;
      }
      if (reconnectTimerRef.current) {
        clearTimeout(reconnectTimerRef.current);
        reconnectTimerRef.current = null;
      }
    };
  }, [run?.status, connectWs]);

  return { run, turns, wsStatus, loading };
}

export default useAgentRun;
