import React, { useEffect, useRef, useState } from 'react';
import {
  Card,
  Table,
  Tag,
  Button,
  Input,
  Space,
  Spin,
  Message,
  Badge,
} from '@arco-design/web-react';
import {
  IconRecord,
  IconStop,
  IconSwap,
  IconRefresh,
} from '@arco-design/web-react/icon';
import axios from 'axios';

/**
 * 录制控制页：启动/停止 CDP 录制、查看录制事件、把事件转换为测试步骤。
 *
 * API 契约（与 app/routers/recordings_router.py 一致）：
 *   POST /api/recordings/start                        -> { session_id, status, ... }
 *   POST /api/recordings/{session_id}/stop            -> { session_id, status, ... }
 *   GET  /api/recordings/{session_id}/events          -> RecordedEvent[]
 *   POST /api/recordings/{session_id}/convert         -> { steps: [{ step_description, expected_result }], ... }
 */

// 录制状态：空闲 / 录制中 / 已停止
type RecordingStatus = 'idle' | 'recording' | 'stopped';

interface RecordedEvent {
  event_type: string;
  timestamp: number;
  selector?: string | null;
  value?: string | null;
  url?: string;
  page_title?: string;
  screenshot?: string | null;
}

interface TestStep {
  step_description: string;
  expected_result: string;
}

// 把秒级时间戳格式化成本地时间字符串
const formatTimestamp = (ts: number | string | null | undefined): string => {
  if (ts === null || ts === undefined || ts === '') return '-';
  const n = typeof ts === 'string' ? Number(ts) : ts;
  if (!Number.isFinite(n) || n <= 0) return '-';
  // 后端 timestamp 是秒；如果是毫秒级（> 1e12），按毫秒处理
  const ms = n > 1e12 ? n : n * 1000;
  return new Date(ms).toLocaleString();
};

// 截断长字符串并保留完整内容到 title 属性
const truncate = (s: string | null | undefined, max = 40): string => {
  if (s === null || s === undefined) return '-';
  const str = String(s);
  return str.length > max ? `${str.slice(0, max)}…` : str;
};

// 简易 axios 错误信息提取
const extractError = (e: unknown, fallback: string): string => {
  const err = e as { response?: { data?: { detail?: string } }; message?: string };
  return err?.response?.data?.detail || err?.message || fallback;
};

// 录制中状态点的脉冲动画（不需要单独的 .less 文件）
const pulseStyle = `
@keyframes recording-pulse {
  0%   { box-shadow: 0 0 0 0 rgba(245, 63, 63, 0.65); }
  70%  { box-shadow: 0 0 0 8px rgba(245, 63, 63, 0); }
  100% { box-shadow: 0 0 0 0 rgba(245, 63, 63, 0); }
}
.recording-pulse-dot {
  display: inline-block;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background-color: #f53f3f;
  animation: recording-pulse 1.4s ease-in-out infinite;
}
`;

const Recordings: React.FC = () => {
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [status, setStatus] = useState<RecordingStatus>('idle');
  const [url, setUrl] = useState('');
  const [events, setEvents] = useState<RecordedEvent[]>([]);
  const [steps, setSteps] = useState<TestStep[]>([]);
  const [loading, setLoading] = useState(false);
  const [converting, setConverting] = useState(false);
  const [eventsLoading, setEventsLoading] = useState(false);

  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // 组件卸载时清理轮询
  useEffect(() => {
    return () => {
      if (pollRef.current) {
        clearInterval(pollRef.current);
        pollRef.current = null;
      }
    };
  }, []);

  // 录制中：每 2 秒拉一次事件流
  useEffect(() => {
    if (status === 'recording' && sessionId) {
      const sid = sessionId;
      const tick = async () => {
        try {
          const res = await axios.get<RecordedEvent[]>(
            `/api/recordings/${sid}/events`
          );
          setEvents(Array.isArray(res.data) ? res.data : []);
        } catch (e) {
          // 轮询中静默失败，避免淹没用户
          console.warn('拉取录制事件失败:', extractError(e, ''));
        }
      };
      // 立刻拉一次，再开启定时器
      tick();
      pollRef.current = setInterval(tick, 2000);
      return () => {
        if (pollRef.current) {
          clearInterval(pollRef.current);
          pollRef.current = null;
        }
      };
    }
    return undefined;
  }, [status, sessionId]);

  const handleStart = async () => {
    if (!url.trim()) {
      Message.warning('请输入录制目标 URL');
      return;
    }
    setLoading(true);
    try {
      const res = await axios.post('/api/recordings/start', {
        url: url.trim(),
        page_title: '',
      });
      setSessionId(res.data.session_id);
      setStatus('recording');
      setEvents([]);
      setSteps([]);
      Message.success('录制已开始');
    } catch (e) {
      Message.error(extractError(e, '启动录制失败'));
    } finally {
      setLoading(false);
    }
  };

  const handleStop = async () => {
    if (!sessionId) return;
    setLoading(true);
    try {
      await axios.post(`/api/recordings/${sessionId}/stop`);
      setStatus('stopped');
      // 停止后做一次最终拉取，确保表格显示完整事件
      try {
        const res = await axios.get<RecordedEvent[]>(
          `/api/recordings/${sessionId}/events`
        );
        setEvents(Array.isArray(res.data) ? res.data : []);
      } catch {
        // 忽略：停止接口已成功
      }
      Message.success('录制已停止');
    } catch (e) {
      Message.error(extractError(e, '停止录制失败'));
    } finally {
      setLoading(false);
    }
  };

  // 手动刷新事件（录制中也可点）
  const refreshEvents = async () => {
    if (!sessionId) return;
    setEventsLoading(true);
    try {
      const res = await axios.get<RecordedEvent[]>(
        `/api/recordings/${sessionId}/events`
      );
      setEvents(Array.isArray(res.data) ? res.data : []);
    } catch (e) {
      Message.error(extractError(e, '刷新事件失败'));
    } finally {
      setEventsLoading(false);
    }
  };

  const handleConvert = async () => {
    if (!sessionId) return;
    setConverting(true);
    try {
      const res = await axios.post(
        `/api/recordings/${sessionId}/convert`,
        { session_id: sessionId }
      );
      const newSteps: TestStep[] = Array.isArray(res.data?.steps)
        ? res.data.steps
        : [];
      setSteps(newSteps);
      Message.success(
        newSteps.length > 0
          ? `已生成 ${newSteps.length} 个测试步骤`
          : '录制事件为空，未生成步骤'
      );
    } catch (e) {
      Message.error(extractError(e, '转换失败'));
    } finally {
      setConverting(false);
    }
  };

  // 状态徽标：空闲（蓝）/ 录制中（红点脉冲）/ 已停止（灰）
  const renderStatusBadge = () => {
    if (status === 'recording') {
      return (
        <Space size={6} align="center">
          <span className="recording-pulse-dot" />
          <Tag color="red" style={{ margin: 0 }}>录制中</Tag>
        </Space>
      );
    }
    if (status === 'stopped') {
      return (
        <Space size={6} align="center">
          <Badge dot dotStyle={{ backgroundColor: '#86909c' }} />
          <Tag color="gray" style={{ margin: 0 }}>已停止</Tag>
        </Space>
      );
    }
    return (
      <Space size={6} align="center">
        <Badge dot dotStyle={{ backgroundColor: '#165dff' }} />
        <Tag color="blue" style={{ margin: 0 }}>空闲</Tag>
      </Space>
    );
  };

  // 事件表格列定义
  const eventColumns = [
    {
      title: '类型',
      dataIndex: 'event_type',
      width: 120,
      render: (v: string) => <Tag>{v || '-'}</Tag>,
    },
    {
      title: '时间',
      dataIndex: 'timestamp',
      width: 180,
      render: (v: number) => formatTimestamp(v),
    },
    {
      title: '选择器',
      dataIndex: 'selector',
      ellipsis: true,
      render: (v: string) => (
        <span title={v || ''} style={{ fontFamily: 'monospace' }}>
          {truncate(v, 40)}
        </span>
      ),
    },
    {
      title: '值',
      dataIndex: 'value',
      width: 200,
      ellipsis: true,
      render: (v: string) => <span title={v || ''}>{truncate(v, 40)}</span>,
    },
    {
      title: 'URL',
      dataIndex: 'url',
      ellipsis: true,
      render: (v: string) => <span title={v || ''}>{truncate(v, 40)}</span>,
    },
  ];

  // 测试步骤列定义
  const stepColumns = [
    {
      title: '#',
      width: 56,
      render: (_: unknown, _r: TestStep, idx: number) => (
        <Tag style={{ minWidth: 28, textAlign: 'center' }}>{idx + 1}</Tag>
      ),
    },
    {
      title: '步骤描述',
      dataIndex: 'step_description',
      render: (v: string) => v || '-',
    },
    {
      title: '预期结果',
      dataIndex: 'expected_result',
      render: (v: string) => v || '-',
    },
  ];

  const hasEvents = events.length > 0;
  const showEventsCard = status !== 'idle' || hasEvents;

  return (
    <>
      {/* 脉冲动画 keyframes —— 全局样式一次 */}
      <style>{pulseStyle}</style>

      <div>
        {/* A. 控制区 */}
        <Card title="录制控制" style={{ marginBottom: 16 }}>
          <Space direction="vertical" size="large" style={{ width: '100%' }}>
            <Space wrap size="medium" align="center">
              <span style={{ minWidth: 72, color: 'var(--color-text-2)' }}>
                目标 URL
              </span>
              <Input
                style={{ width: 480 }}
                placeholder="请输入录制目标 URL"
                value={url}
                onChange={(v) => setUrl(v)}
                disabled={status === 'recording'}
                allowClear
              />
              <Button
                type="primary"
                status="success"
                loading={loading}
                disabled={status === 'recording'}
                icon={<IconRecord />}
                onClick={handleStart}
              >
                开始录制
              </Button>
              {status === 'recording' && (
                <Button
                  status="danger"
                  loading={loading}
                  icon={<IconStop />}
                  onClick={handleStop}
                >
                  停止录制
                </Button>
              )}
            </Space>

            <Space wrap size="medium" align="center">
              <span style={{ minWidth: 72, color: 'var(--color-text-2)' }}>
                状态
              </span>
              {renderStatusBadge()}
              {sessionId && (
                <span
                  style={{
                    color: 'var(--color-text-3)',
                    fontSize: 13,
                    fontFamily: 'monospace',
                  }}
                  title={sessionId}
                >
                  会话 ID: {sessionId}
                </span>
              )}
            </Space>
          </Space>
        </Card>

        {/* B. 事件区 */}
        {showEventsCard && (
          <Card
            title={
              <Space>
                <span>录制事件</span>
                {status === 'recording' && (
                  <Button
                    size="mini"
                    icon={<IconRefresh />}
                    onClick={refreshEvents}
                    loading={eventsLoading}
                  >
                    刷新
                  </Button>
                )}
              </Space>
            }
            style={{ marginBottom: 16 }}
          >
            <div
              style={{
                marginBottom: 12,
                color: 'var(--color-text-2)',
                fontSize: 13,
              }}
            >
              已录制 {events.length} 个事件
              {status === 'recording' && ' · 每 2 秒自动刷新'}
            </div>
            <Table
              columns={eventColumns}
              data={events}
              rowKey={(record: RecordedEvent, index?: number) =>
                `${record.timestamp}-${index ?? 0}`
              }
              pagination={{ pageSize: 20, showTotal: true }}
              scroll={{ x: 900 }}
              noDataElement={
                <div style={{ padding: 24, color: 'var(--color-text-3)' }}>
                  {status === 'recording'
                    ? '暂无录制事件，请在浏览器中操作目标页面…'
                    : '暂无录制事件'}
                </div>
              }
            />
          </Card>
        )}

        {/* C. 转换区 */}
        {hasEvents && (
          <Card title="转换为测试步骤">
            <Space
              direction="vertical"
              size="medium"
              style={{ width: '100%' }}
            >
              <Space wrap size="medium" align="center">
                <Button
                  type="primary"
                  icon={<IconSwap />}
                  loading={converting}
                  onClick={handleConvert}
                  disabled={status === 'recording'}
                >
                  转换为测试步骤
                </Button>
                {converting && <Spin />}
                {steps.length > 0 && (
                  <span style={{ color: 'var(--color-text-2)' }}>
                    共 {steps.length} 个测试步骤
                  </span>
                )}
              </Space>

              {steps.length > 0 && (
                <Table
                  columns={stepColumns}
                  data={steps}
                  rowKey={(_, idx) => `step-${idx}`}
                  pagination={false}
                />
              )}
            </Space>
          </Card>
        )}
      </div>
    </>
  );
};

export default Recordings;