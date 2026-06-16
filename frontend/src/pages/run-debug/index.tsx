import React, { useState, useEffect, useRef, useCallback, useMemo } from 'react';
import {
  Button, Tag, Modal, Input, Message, Spin, Space, Typography,
} from '@arco-design/web-react';
import {
  IconPlayArrow, IconPause, IconCheck, IconClose, IconLoading,
  IconSync, IconSkipNext, IconEdit, IconStop, IconInfoCircle,
} from '@arco-design/web-react/icon';
import { useParams, useLocation } from 'react-router-dom';
import axios from 'axios';
import styles from './style/index.module.less';

const { Text } = Typography;

/* ========== 类型定义 ========== */

type StepStatus = 'pending' | 'running' | 'passed' | 'failed' | 'skipped';

interface StepInfo {
  id?: number;
  step_order: number;
  description: string;
  healed_selector?: string;
  status: StepStatus;
  duration?: number;
  error?: string;
  screenshot_path?: string;
  logs: string[];
}

interface CaseData {
  id: number;
  name: string;
  description?: string;
  steps: { id: number; step_order: number; description: string; healed_selector?: string }[];
}

interface WsStepStart {
  type: 'step_start';
  timestamp: string;
  step_id: number;
  message: string;
}

interface WsStepComplete {
  type: 'step_complete';
  timestamp: string;
  step_id: number;
  status: string;
  duration: number;
}

interface WsExecutionPaused {
  type: 'execution_paused';
  run_id: number;
  step_id: number;
  step_description: string;
  reason: string;
  options: string[];
}

interface WsExecutionResumed {
  type: 'execution_resumed';
  run_id: number;
  step_id: number;
  decision: string;
  new_description?: string;
}

type WsMessage = WsStepStart | WsStepComplete | WsExecutionPaused | WsExecutionResumed;

type ExecutionPhase = 'idle' | 'running' | 'paused' | 'completed';

/* ========== 常量 ========== */

const STATUS_LABELS: Record<StepStatus, string> = {
  pending: '等待',
  running: '执行中',
  passed: '通过',
  failed: '失败',
  skipped: '跳过',
};

const STATUS_COLORS: Record<StepStatus, string> = {
  pending: 'gray',
  running: 'blue',
  passed: 'green',
  failed: 'red',
  skipped: 'orange',
};

/* ========== 子组件 ========== */

/** 步骤状态图标 */
const StepStatusIcon: React.FC<{ status: StepStatus }> = ({ status }) => {
  switch (status) {
    case 'pending':
      return <span className={`${styles['step-status-icon']} ${styles.pending}`}>○</span>;
    case 'running':
      return (
        <span className={`${styles['step-status-icon']} ${styles.running}`}>
          <IconLoading spin style={{ fontSize: 12 }} />
        </span>
      );
    case 'passed':
      return <span className={`${styles['step-status-icon']} ${styles.passed}`}>✓</span>;
    case 'failed':
      return <span className={`${styles['step-status-icon']} ${styles.failed}`}>✗</span>;
    case 'skipped':
      return <span className={`${styles['step-status-icon']} ${styles.skipped}`}>→</span>;
    default:
      return <span className={`${styles['step-status-icon']} ${styles.pending}`}>○</span>;
  }
};

/* ========== 主页面组件 ========== */

const RunDebugPage: React.FC = () => {
  /* --- URL 参数解析（同时支持路径参数 /:runId 和查询参数 ?runId=） --- */
  const { runId: routeRunId } = useParams<{ runId: string }>();
  const location = useLocation();

  const getUrlParam = useCallback(
    (key: string): string | null => {
      if (key === 'runId' && routeRunId) return routeRunId;
      const params = new URLSearchParams(location.search);
      return params.get(key);
    },
    [routeRunId, location.search],
  );

  const runId = getUrlParam('runId');
  const caseId = getUrlParam('caseId');

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
  const [editDescription, setEditDescription] = useState('');

  // WebSocket 引用
  const wsRef = useRef<WebSocket | null>(null);
  // 跟踪 step_id -> phase 中是否已收到该步骤的完成消息（避免重复标记 completed）
  const phaseRef = useRef(phase);
  phaseRef.current = phase;

  /* --- 获取测试用例数据 --- */
  const fetchCaseData = useCallback(async (cid: number) => {
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
      Message.error('加载用例数据失败: ' + (e.response?.data?.detail || '未知错误'));
    } finally {
      setLoading(false);
    }
  }, []);

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
  }, [runId]);

  /* --- 处理 WebSocket 消息 --- */
  const handleWsMessage = useCallback((msg: WsMessage) => {
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
        setPauseReason(msg.reason || '执行暂停');
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
  }, []);

  /* --- 控制指令：发送到 WebSocket --- */
  const sendControl = useCallback(
    (action: string, payload?: Record<string, unknown>) => {
      if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) {
        Message.warning('WebSocket 未连接，无法发送指令');
        return;
      }
      wsRef.current.send(JSON.stringify({ type: 'control', action, ...payload }));
    },
    [],
  );

  /* --- 事件处理 --- */
  const handleRetry = () => sendControl('retry');
  const handleSkip = () => sendControl('skip');
  const handleAbort = () => sendControl('abort');
  const handleEditOpen = () => {
    setEditDescription(pauseStepDesc);
    setEditVisible(true);
  };
  const handleEditSubmit = () => {
    if (!editDescription.trim()) {
      Message.warning('步骤描述不能为空');
      return;
    }
    sendControl('edit', { new_description: editDescription.trim() });
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

  /* --- 渲染 --- */
  if (loading) {
    return (
      <div className={styles.container}>
        <Spin
          loading
          tip="加载用例数据..."
          style={{ display: 'block', margin: '120px auto' }}
        />
      </div>
    );
  }

  if (!runId) {
    return (
      <div className={styles.container}>
        <div className={styles['empty-state']}>
          <IconInfoCircle style={{ fontSize: 48, color: 'var(--color-text-4)' }} />
          <Text>缺少 runId 参数，请通过 /run-debug/:runId 访问</Text>
        </div>
      </div>
    );
  }

  return (
    <div className={styles.container}>
      {/* ===== 顶部信息栏 ===== */}
      <div className={styles['header-bar']}>
        <div className={styles['header-left']}>
          <span className={styles['case-name']}>
            {caseData?.name || (caseId ? '加载中...' : '实时执行监控')}
          </span>
          <span className={styles['run-id']}>run#{runId}</span>
        </div>
        <div className={styles['header-right']}>
          <div className={styles['ws-indicator']}>
            <span
              className={`${styles['ws-dot']} ${
                wsConnected ? styles.connected : styles.disconnected
              }`}
            />
            {wsConnected ? '已连接' : '未连接'}
          </div>
          <Tag
            color={
              phase === 'running'
                ? 'blue'
                : phase === 'paused'
                  ? 'orange'
                  : phase === 'completed'
                    ? 'green'
                    : 'gray'
            }
          >
            {phase === 'running' && <IconLoading spin style={{ marginRight: 4 }} />}
            {phase === 'running'
              ? '运行中'
              : phase === 'paused'
                ? '已暂停'
                : phase === 'completed'
                  ? '已完成'
                  : '等待中'}
          </Tag>
        </div>
      </div>

      {/* ===== 主内容区（左右两栏） ===== */}
      <div className={styles['main-content']}>
        {/* --- 左侧：步骤列表 --- */}
        <div className={styles['step-panel']}>
          <div className={styles['step-panel-header']}>
            执行步骤 ({stats.total})
          </div>
          <div className={styles['step-list']}>
            {steps.length === 0 && !caseId && (
              <div className={styles['empty-state']} style={{ padding: 40 }}>
                <IconInfoCircle style={{ fontSize: 32, color: 'var(--color-text-4)' }} />
                <Text style={{ fontSize: 13 }}>等待接收执行数据...</Text>
              </div>
            )}
            {steps.map((step, idx) => {
              const isCurrent = step.status === 'running';
              const isSelected = selectedStepIdx === idx;
              return (
                <div
                  key={step.id || step.step_order}
                  className={`${styles['step-item']} ${
                    isCurrent ? styles.active : ''
                  } ${isSelected ? styles.selected : ''}`}
                  onClick={() => setSelectedStepIdx(idx)}
                  role="button"
                  tabIndex={0}
                  onKeyDown={(e) => e.key === 'Enter' && setSelectedStepIdx(idx)}
                >
                  <StepStatusIcon status={step.status} />
                  <div className={styles['step-content']}>
                    <div className={styles['step-order']}>
                      步骤 {step.step_order}
                    </div>
                    <div className={styles['step-desc']}>{step.description}</div>
                    {step.healed_selector && (
                      <Text type="secondary" style={{ fontSize: 12, color: 'var(--color-text-3)', marginTop: 2 }}>
                        🔧 已修复: {step.healed_selector}
                      </Text>
                    )}
                  </div>
                  {step.duration != null && (
                    <span className={styles['step-duration']}>
                      {step.duration.toFixed(1)}s
                    </span>
                  )}
                </div>
              );
            })}
          </div>
        </div>

        {/* --- 右侧：详情与日志区 --- */}
        <div className={styles['detail-panel']}>
          {/* 暂停时的控制按钮 */}
          {phase === 'paused' && (
            <div className={styles['control-bar']}>
              <div className={styles['pause-reason']}>
                暂停原因: {pauseReason}
                {pauseStepDesc && `（步骤: ${pauseStepDesc}）`}
              </div>
              <Button
                type="primary"
                className={styles['control-btn']}
                icon={<IconSync />}
                onClick={handleRetry}
              >
                重试
              </Button>
              <Button
                type="primary"
                status="warning"
                className={styles['control-btn']}
                icon={<IconSkipNext />}
                onClick={handleSkip}
              >
                跳过
              </Button>
              <Button
                type="primary"
                status="danger"
                className={styles['control-btn']}
                icon={<IconStop />}
                onClick={handleAbort}
              >
                中止
              </Button>
              <Button
                type="outline"
                className={styles['control-btn']}
                icon={<IconEdit />}
                onClick={handleEditOpen}
              >
                编辑
              </Button>
            </div>
          )}

          {/* 详情头部 */}
          <div className={styles['detail-header']}>
            {selectedStep ? (
              <>
                <StepStatusIcon status={selectedStep.status} />
                <span>步骤 {selectedStep.step_order} 详情</span>
                {selectedStep.duration != null && (
                  <Tag size="small" color={STATUS_COLORS[selectedStep.status]}>
                    {STATUS_LABELS[selectedStep.status]} ·{' '}
                    {selectedStep.duration.toFixed(1)}s
                  </Tag>
                )}
              </>
            ) : (
              <Space>
                {phase === 'running' && (
                  <IconPlayArrow style={{ color: 'var(--color-primary-6)' }} />
                )}
                {phase === 'paused' && (
                  <IconPause style={{ color: 'var(--color-warning-6)' }} />
                )}
                {phase === 'completed' && (
                  <IconCheck style={{ color: 'var(--color-success-6)' }} />
                )}
                {phase === 'running'
                  ? '实时监控'
                  : phase === 'paused'
                    ? '执行已暂停'
                    : phase === 'completed'
                      ? '执行结果摘要'
                      : '选择一个步骤查看详情'}
              </Space>
            )}
          </div>

          {/* 详情内容 */}
          <div className={styles['detail-body']}>
            {/* 运行中状态 */}
            {phase === 'running' && !selectedStep && (
              <div className={styles['running-state']}>
                <div className={styles['running-spinner']}>
                  <IconLoading spin />
                </div>
                <div className={styles['running-text']}>测试执行中...</div>
                {currentStepIdx >= 0 && (
                  <div className={styles['current-step-hint']}>
                    正在执行: 步骤 {steps[currentStepIdx]?.step_order} -{' '}
                    {steps[currentStepIdx]?.description}
                  </div>
                )}
                {steps.length > 0 && (
                  <div className={styles['summary-stats']}>
                    <div className={styles['stat-item']}>
                      <div className={`${styles['stat-value']} ${styles['stat-passed']}`}>
                        {stats.passed}
                      </div>
                      <div className={styles['stat-label']}>通过</div>
                    </div>
                    <div className={styles['stat-item']}>
                      <div className={`${styles['stat-value']} ${styles['stat-failed']}`}>
                        {stats.failed}
                      </div>
                      <div className={styles['stat-label']}>失败</div>
                    </div>
                    <div className={styles['stat-item']}>
                      <div className={`${styles['stat-value']} ${styles['stat-pending']}`}>
                        {stats.pending}
                      </div>
                      <div className={styles['stat-label']}>剩余</div>
                    </div>
                  </div>
                )}
              </div>
            )}

            {/* 完成状态 */}
            {phase === 'completed' && !selectedStep && (
              <div className={styles['completed-state']}>
                <div className={styles['completed-icon']}>
                  {stats.failed === 0 ? (
                    <IconCheck style={{ color: 'var(--color-success-6)' }} />
                  ) : (
                    <IconClose style={{ color: 'var(--color-danger-6)' }} />
                  )}
                </div>
                <Text style={{ fontSize: 18, fontWeight: 600 }}>
                  {stats.failed === 0 ? '全部通过！' : '执行完成（含失败）'}
                </Text>
                <div className={styles['summary-stats']}>
                  <div className={styles['stat-item']}>
                    <div className={`${styles['stat-value']} ${styles['stat-passed']}`}>
                      {stats.passed}
                    </div>
                    <div className={styles['stat-label']}>通过</div>
                  </div>
                  <div className={styles['stat-item']}>
                    <div className={`${styles['stat-value']} ${styles['stat-failed']}`}>
                      {stats.failed}
                    </div>
                    <div className={styles['stat-label']}>失败</div>
                  </div>
                  <div className={styles['stat-item']}>
                    <div className={`${styles['stat-value']} ${styles['stat-pending']}`}>
                      {stats.total}
                    </div>
                    <div className={styles['stat-label']}>总计</div>
                  </div>
                </div>
              </div>
            )}

            {/* 暂停时无选中步骤 */}
            {phase === 'paused' && !selectedStep && (
              <div className={styles['running-state']}>
                <IconPause style={{ fontSize: 40, color: 'var(--color-warning-6)' }} />
                <Text style={{ fontSize: 14, color: 'var(--color-text-2)' }}>
                  执行已暂停：{pauseReason}
                </Text>
                <Text style={{ fontSize: 12, color: 'var(--color-text-3)' }}>
                  请在上方选择操作：重试 / 跳过 / 中止 / 编辑
                </Text>
              </div>
            )}

            {/* 已选中步骤的详情 */}
            {selectedStep && (
              <>
                <div style={{ marginBottom: 16 }}>
                  <Text
                    style={{
                      fontSize: 15,
                      fontWeight: 600,
                      display: 'block',
                      marginBottom: 4,
                    }}
                  >
                    步骤 {selectedStep.step_order}
                  </Text>
                  <Text
                    style={{
                      color: 'var(--color-text-2)',
                      lineHeight: 1.7,
                      whiteSpace: 'pre-wrap',
                    }}
                  >
                    {selectedStep.description}
                  </Text>
                  {selectedStep.healed_selector && (
                    <Text type="secondary" style={{ fontSize: 12, color: 'var(--color-text-3)', display: 'block', marginTop: 4 }}>
                      🔧 已修复: {selectedStep.healed_selector}
                    </Text>
                  )}
                </div>

                {selectedStep.error && (
                  <div style={{ marginBottom: 12 }}>
                    <Text
                      style={{
                        fontWeight: 600,
                        fontSize: 13,
                        color: 'var(--color-danger-6)',
                      }}
                    >
                      错误信息：
                    </Text>
                    <div className={styles['log-error']}>{selectedStep.error}</div>
                  </div>
                )}

                {selectedStep.screenshot_path ? (
                  <div style={{ marginBottom: 12 }}>
                    <Text style={{ fontWeight: 600, fontSize: 13 }}>
                      步骤截图：
                    </Text>
                    <img
                      src={`/${selectedStep.screenshot_path}`}
                      alt={`步骤 ${selectedStep.step_order} 截图`}
                      style={{
                        maxWidth: '100%',
                        borderRadius: 6,
                        border: '1px solid var(--color-border-2)',
                        marginTop: 8,
                      }}
                    />
                  </div>
                ) : (
                  <div className={styles['screenshot-placeholder']}>
                    <IconInfoCircle style={{ fontSize: 24 }} />
                    <span>暂无截图</span>
                  </div>
                )}

                {selectedStep.logs.length > 0 && (
                  <div style={{ marginTop: 12 }}>
                    <Text
                      style={{
                        fontWeight: 600,
                        fontSize: 13,
                        display: 'block',
                        marginBottom: 8,
                      }}
                    >
                      步骤日志：
                    </Text>
                    <div className={styles['log-area']}>
                      {selectedStep.logs.map((log, i) => (
                        <div key={i} className={styles['log-entry']}>
                          {log}
                        </div>
                      ))}
                    </div>
                  </div>
                )}
              </>
            )}
          </div>
        </div>
      </div>

      {/* ===== 编辑步骤描述弹窗 ===== */}
      <Modal
        title="编辑步骤描述"
        visible={editVisible}
        onOk={handleEditSubmit}
        onCancel={() => setEditVisible(false)}
        okText="确认修改"
        cancelText="取消"
        unmountOnExit
        style={{ width: 500 }}
      >
        <div className={styles['edit-modal-text']}>
          修改步骤描述后将重新执行该步骤：
        </div>
        <Input.TextArea
          value={editDescription}
          onChange={(v) => setEditDescription(v)}
          placeholder="输入新的步骤描述..."
          autoSize={{ minRows: 3, maxRows: 8 }}
        />
      </Modal>
    </div>
  );
};

export default RunDebugPage;
