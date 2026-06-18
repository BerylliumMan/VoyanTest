import { useState, useEffect, useRef, useCallback, useMemo } from 'react';
import { Form, Message } from '@arco-design/web-react';
import axios from 'axios';
import {
  CaseData,
  ExecutionPhase,
  StepInfo,
  StepStatus,
  WsMessage,
} from './types';

/**
 * run-debug 页面核心逻辑：状态 + WebSocket + 事件处理 + 计算属性
 */
export function useRunDebug(
  runId: string | null,
  caseId: string | null,
  t: Record<string, string>,
) {
  /* --- 状态 --- */
  const [caseData, setCaseData] = useState<CaseData | null>(null);
  const [steps, setSteps] = useState<StepInfo[]>([]);
  const [phase, setPhase] = useState<ExecutionPhase>('idle');
  const [wsConnected, setWsConnected] = useState(false);
  const [selectedStepIdx, setSelectedStepIdx] = useState<number | null>(null);
  const [loading, setLoading] = useState(false);

  // 暂停相关
  const [pauseReason, setPauseReason] = useState('');
  const [pauseStepId, setPauseStepId] = useState<number | null>(null);
  const [pauseStepDesc, setPauseStepDesc] = useState('');

  // 编辑弹窗
  const [editVisible, setEditVisible] = useState(false);
  const [editForm] = Form.useForm();

  // WebSocket 引用
  const wsRef = useRef<WebSocket | null>(null);
  // 跟踪 step_id -> phase 中是否已收到该步骤的完成消息（避免重复标记 completed）
  const phaseRef = useRef(phase);
  phaseRef.current = phase;

  /* --- 获取测试用例数据 --- */
  const fetchCaseData = useCallback(
    async (cid: number) => {
      setLoading(true);
      try {
        const res = await axios.get(`/api/test-cases/${cid}`);
        const data = res.data as CaseData;
        setCaseData(data);
        // 用 API 返回的步骤初始化步骤列表
        const initialSteps: StepInfo[] = (data.steps || []).map((s) => ({
          id: s.id,
          step_order: s.step_order,
          description: s.description,
          healed_selector: s.healed_selector,
          status: 'pending' as StepStatus,
          logs: [],
        }));
        setSteps(initialSteps);
      } catch (err: unknown) {
        const e = err as { response?: { data?: { detail?: string } } };
        const detail = e.response?.data?.detail || t['debug.unknown_error'];
        Message.error(t['debug.load_case_failed'].replace('{detail}', detail));
      } finally {
        setLoading(false);
      }
    },
    [t],
  );

  useEffect(() => {
    if (caseId) {
      fetchCaseData(Number(caseId));
    }
  }, [caseId, fetchCaseData]);

  /* --- WebSocket 连接 --- */
  useEffect(() => {
    if (!runId) return;

    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl = `${protocol}//${window.location.host}/ws/logs/${runId}`;
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
    let destroyed = false;

    const connect = () => {
      if (destroyed) return;
      if (wsRef.current?.readyState === WebSocket.OPEN) return;

      const ws = new WebSocket(wsUrl);
      wsRef.current = ws;

      ws.onopen = () => {
        setWsConnected(true);
        setPhase((prev) => (prev === 'idle' ? 'running' : prev));
      };

      ws.onmessage = (event) => {
        try {
          const msg: WsMessage = JSON.parse(event.data);
          handleWsMessage(msg);
        } catch {
          // 忽略解析错误
        }
      };

      ws.onclose = () => {
        setWsConnected(false);
        wsRef.current = null;
        // 完成状态不重连
        if (!destroyed && phaseRef.current !== 'completed') {
          reconnectTimer = setTimeout(connect, 3000);
        }
      };

      ws.onerror = () => {
        setWsConnected(false);
        wsRef.current = null;
        if (!destroyed && phaseRef.current !== 'completed') {
          reconnectTimer = setTimeout(connect, 3000);
        }
      };
    };

    connect();

    return () => {
      destroyed = true;
      if (reconnectTimer) clearTimeout(reconnectTimer);
      if (wsRef.current) {
        wsRef.current.close();
        wsRef.current = null;
      }
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runId]);

  /* --- 处理 WebSocket 消息 --- */
  const handleWsMessage = useCallback(
    (msg: WsMessage) => {
      switch (msg.type) {
        case 'step_start': {
          // 将对应步骤设为 running
          setSteps((prev) =>
            prev.map((s) =>
              s.id === msg.step_id || s.step_order === msg.step_id
                ? { ...s, status: 'running', logs: [`[${msg.timestamp}] ${msg.message}`] }
                : s,
            ),
          );
          break;
        }
        case 'step_complete': {
          const newStatus: StepStatus =
            msg.status === 'passed'
              ? 'passed'
              : msg.status === 'failed'
                ? 'failed'
                : msg.status === 'skipped'
                  ? 'skipped'
                  : 'passed';
          setSteps((prev) => {
            const next = prev.map((s) =>
              s.id === msg.step_id || s.step_order === msg.step_id
                ? { ...s, status: newStatus, duration: msg.duration }
                : s,
            );
            // 检查是否所有非 pending/running 都已完成
            const allDone = next.every(
              (s) => s.status !== 'pending' && s.status !== 'running',
            );
            if (allDone && next.length > 0) {
              setPhase('completed');
            }
            return next;
          });
          break;
        }
        case 'execution_paused': {
          setPhase('paused');
          setPauseReason(msg.reason || t['debug.paused_default_reason']);
          setPauseStepId(msg.step_id);
          setPauseStepDesc(msg.step_description || '');
          break;
        }
        case 'execution_resumed': {
          setPhase('running');
          setPauseReason('');
          setPauseStepId(null);
          setPauseStepDesc('');
          // 如果 back-end 返回了新的步骤描述，更新对应步骤
          if (msg.new_description) {
            setSteps((prev) =>
              prev.map((s) =>
                s.id === msg.step_id || s.step_order === msg.step_id
                  ? { ...s, description: msg.new_description! }
                  : s,
              ),
            );
          }
          break;
        }
        default:
          break;
      }
    },
    [t],
  );

  /* --- 控制指令：发送到 WebSocket --- */
  const sendControl = useCallback(
    (action: string, payload?: Record<string, unknown>) => {
      if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) {
        Message.warning(t['debug.ws_disconnected']);
        return;
      }
      wsRef.current.send(JSON.stringify({ type: 'control', action, ...payload }));
    },
    [t],
  );

  /* --- 事件处理 --- */
  const handleRetry = () => sendControl('retry');
  const handleSkip = () => sendControl('skip');
  const handleAbort = () => sendControl('abort');
  const handleEditOpen = () => {
    editForm.setFieldsValue({ new_description: pauseStepDesc });
    setEditVisible(true);
  };
  const handleEditSubmit = async () => {
    const values = await editForm.validate();
    sendControl('edit', { new_description: values.new_description.trim() });
    setEditVisible(false);
  };

  /* --- 计算属性 --- */
  const currentStepIdx = useMemo(() => {
    return steps.findIndex((s) => s.status === 'running');
  }, [steps]);

  const stats = useMemo(() => {
    const passed = steps.filter((s) => s.status === 'passed').length;
    const failed = steps.filter((s) => s.status === 'failed').length;
    const pending = steps.filter(
      (s) => s.status === 'pending' || s.status === 'running',
    ).length;
    return { passed, failed, pending, total: steps.length };
  }, [steps]);

  const selectedStep = selectedStepIdx != null ? steps[selectedStepIdx] : null;

  return {
    caseData,
    steps,
    phase,
    wsConnected,
    selectedStepIdx,
    setSelectedStepIdx,
    loading,
    pauseReason,
    pauseStepDesc,
    editVisible,
    setEditVisible,
    editForm,
    currentStepIdx,
    stats,
    selectedStep,
    fetchCaseData,
    handleRetry,
    handleSkip,
    handleAbort,
    handleEditOpen,
    handleEditSubmit,
  };
}
