import { useState, useEffect, useRef, useCallback } from 'react';
import { apiRequest } from '@/utils/apiRequest';
import useLocale from '@/utils/useLocale';

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

type RecordingStatus = 'idle' | 'recording' | 'stopped';

/**
 * 录制控制页业务逻辑 Hook：
 *   - 管理 session / status / events / steps 等状态
 *   - 启动 / 停止录制、轮询事件流、转换为测试步骤
 *
 * UI 通知（Message.success / error / warning）由调用方决定,
 * 本 Hook 仅返回 boolean 表示操作是否成功。
 */
export function useRecordings() {
  const t = useLocale();
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [status, setStatus] = useState<RecordingStatus>('idle');
  const [url, setUrl] = useState('');
  const [events, setEvents] = useState<RecordedEvent[]>([]);
  const [steps, setSteps] = useState<TestStep[]>([]);
  const [loading, setLoading] = useState(false);
  const [converting, setConverting] = useState(false);

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
          const data = await apiRequest<RecordedEvent[]>(
            { method: 'GET', url: `/api/recordings/${sid}/events` },
            { showSuccess: false, showError: false }
          );
          setEvents(Array.isArray(data) ? data : []);
        } catch {
          // 轮询中静默失败
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

  const startRecording = useCallback(
    async (targetUrl: string): Promise<boolean> => {
      if (!targetUrl.trim()) {
        // 调用方应已校验，这里仍做一次兜底
        return false;
      }
      setLoading(true);
      try {
        const data = await apiRequest<{ session_id: string }>(
          {
            method: 'POST',
            url: '/api/recordings/start',
            data: { url: targetUrl.trim(), page_title: '' },
          },
          { showError: false }
        );
        setSessionId(data.session_id);
        setStatus('recording');
        setEvents([]);
        setSteps([]);
        return true;
      } catch {
        return false;
      } finally {
        setLoading(false);
      }
    },
    []
  );

  const stopRecording = useCallback(async (): Promise<boolean> => {
    if (!sessionId) return false;
    setLoading(true);
    try {
      await apiRequest(
        { method: 'POST', url: `/api/recordings/${sessionId}/stop` },
        { showError: false }
      );
      setStatus('stopped');
      // 停止后做一次最终拉取
      try {
        const data = await apiRequest<RecordedEvent[]>(
          { method: 'GET', url: `/api/recordings/${sessionId}/events` },
          { showSuccess: false, showError: false }
        );
        setEvents(Array.isArray(data) ? data : []);
      } catch {
        // 忽略：停止接口已成功
      }
      return true;
    } catch {
      return false;
    } finally {
      setLoading(false);
    }
  }, [sessionId]);

  const refreshEvents = useCallback(async (): Promise<boolean> => {
    if (!sessionId) return false;
    try {
      const data = await apiRequest<RecordedEvent[]>(
        { method: 'GET', url: `/api/recordings/${sessionId}/events` },
        { showSuccess: false, showError: false }
      );
      setEvents(Array.isArray(data) ? data : []);
      return true;
    } catch {
      return false;
    }
  }, [sessionId]);

  const convertToSteps = useCallback(async (): Promise<boolean> => {
    if (!sessionId) return false;
    setConverting(true);
    try {
      const data = await apiRequest<{ steps?: TestStep[] }>(
        {
          method: 'POST',
          url: `/api/recordings/${sessionId}/convert`,
          data: { session_id: sessionId },
        },
        { showError: false }
      );
      const newSteps: TestStep[] = Array.isArray(data?.steps) ? data.steps : [];
      setSteps(newSteps);
      return true;
    } catch {
      return false;
    } finally {
      setConverting(false);
    }
  }, [sessionId]);

  return {
    t,
    sessionId,
    status,
    url,
    setUrl,
    events,
    steps,
    loading,
    converting,
    startRecording,
    stopRecording,
    refreshEvents,
    convertToSteps,
  };
}

export type { RecordedEvent, TestStep, RecordingStatus };