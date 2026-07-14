import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import React, { useEffect } from 'react';
import { render, act, cleanup } from '@testing-library/react';
import axios from 'axios';
import { useRecordings } from '@/pages/recordings/hooks';

/* --- Mocks ---------------------------------------------------------------- */

/* 模拟 axios（与 StepList.test.tsx 一致的工厂写法）。
 * useRecordings 经 apiRequest 走的是 axios(config) 而非 axios.get/post，
 * 因此 mock 既要可调用，又要保留 get/post/put/delete 四个具名方法。 */
vi.mock('axios', () => {
  const axiosGet = vi.fn();
  const axiosPost = vi.fn();
  const axiosPut = vi.fn();
  const axiosDelete = vi.fn();
  const axiosDefault = vi.fn();
  const callable: any = (config: any) => axiosDefault(config);
  callable.get = axiosGet;
  callable.post = axiosPost;
  callable.put = axiosPut;
  callable.delete = axiosDelete;
  callable.__mocks = { axiosGet, axiosPost, axiosPut, axiosDelete, axiosDefault };
  return { default: callable };
});

/* 模拟 useLocale，避免引入 GlobalContext 等运行时依赖 */
vi.mock('@/utils/useLocale', () => ({
  default: () =>
    ({
      'recordings.started': '录制已启动',
      'recordings.start_failed': '启动录制失败',
      'recordings.stopped_msg': '录制已停止',
      'recordings.stop_failed': '停止录制失败',
      'recordings.refresh_failed': '刷新事件失败',
      'recordings.convert_failed': '转换为测试步骤失败',
      'recordings.url_required': '请输入目标 URL',
      'recordings.steps_generated': '已生成 {count} 个步骤',
      'recordings.steps_empty': '未生成任何步骤',
    } as Record<string, string>),
}));

const mockedAxios = axios as unknown as {
  get: ReturnType<typeof vi.fn>;
  post: ReturnType<typeof vi.fn>;
};
const mocks = (axios as any).__mocks as {
  axiosGet: ReturnType<typeof vi.fn>;
  axiosPost: ReturnType<typeof vi.fn>;
  axiosPut: ReturnType<typeof vi.fn>;
  axiosDelete: ReturnType<typeof vi.fn>;
  axiosDefault: ReturnType<typeof vi.fn>;
};

/* 让 axios.get / axios.post 复用 axiosDefault 的 mock 队列，
 * 这样测试中 mockResolvedValueOnce 设置的响应可以同时拦截具名调用和 axios(config) 调用。 */
mockedAxios.get = mocks.axiosDefault as any;
mockedAxios.post = mocks.axiosDefault as any;

/* --- Test wrapper --------------------------------------------------------- */

/**
 * 用一个 React 组件把 hook 的返回值塞到 ref 上，方便测试中读取和触发更新。
 * 必须把 ref 初始化为 unknown 类型，避免初次渲染为 undefined 时类型收窄失败。
 */
interface HookReturn {
  t: Record<string, string>;
  sessionId: string | null;
  status: 'idle' | 'recording' | 'stopped';
  url: string;
  events: Array<{
    event_type: string;
    timestamp: number;
    selector?: string | null;
    value?: string | null;
    url?: string;
  }>;
  steps: Array<{ step_description: string; expected_result: string }>;
  loading: boolean;
  converting: boolean;
  startRecording: (targetUrl: string) => Promise<boolean>;
  stopRecording: () => Promise<boolean>;
  refreshEvents: () => Promise<boolean>;
  convertToSteps: () => Promise<boolean>;
}

interface HookHandle {
  current: HookReturn | null;
}

interface HookProbeProps {
  handleRef: HookHandle;
}

const HookProbe: React.FC<HookProbeProps> = ({ handleRef }) => {
  const hook = useRecordings() as unknown as HookReturn;
  useEffect(() => {
    handleRef.current = hook;
  });
  return null;
};

const mountHook = () => {
  const handleRef: HookHandle = { current: null };
  const utils = render(React.createElement(HookProbe, { handleRef }));
  const get = (): HookReturn => {
    if (!handleRef.current) throw new Error('hook not initialised');
    return handleRef.current;
  };
  return { ...utils, get };
};

/* --- Tests ---------------------------------------------------------------- */

describe('useRecordings', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  afterEach(() => {
    cleanup();
  });

  it('初始状态：idle、无 session、events/steps 为空', () => {
    const { get } = mountHook();
    const h = get();
    expect(h.status).toBe('idle');
    expect(h.sessionId).toBeNull();
    expect(h.url).toBe('');
    expect(h.events).toEqual([]);
    expect(h.steps).toEqual([]);
    expect(h.loading).toBe(false);
    expect(h.converting).toBe(false);
  });

  it('startRecording 成功后创建 session，状态变为 recording', async () => {
    mockedAxios.post.mockResolvedValueOnce({
      data: { session_id: 'sess-abc-123', status: 'recording' },
    });

    const { get } = mountHook();
    let ok = false;
    await act(async () => {
      ok = await get().startRecording('https://example.com/login');
    });

    expect(ok).toBe(true);
    expect(mockedAxios.post).toHaveBeenCalledWith(
      expect.objectContaining({
        method: 'POST',
        url: '/api/recordings/start',
        data: { url: 'https://example.com/login', page_title: '' },
      })
    );
    expect(get().sessionId).toBe('sess-abc-123');
    expect(get().status).toBe('recording');
    expect(get().loading).toBe(false);
  });

  it('startRecording 失败时返回 false，状态保持 idle', async () => {
    mockedAxios.post.mockRejectedValueOnce(new Error('network down'));

    const { get } = mountHook();
    let ok = true;
    await act(async () => {
      ok = await get().startRecording('https://example.com');
    });

    expect(ok).toBe(false);
    expect(get().sessionId).toBeNull();
    expect(get().status).toBe('idle');
    expect(get().loading).toBe(false);
  });

  it('startRecording 对空 URL 直接返回 false，不调用后端', async () => {
    const { get } = mountHook();
    let ok = true;
    await act(async () => {
      ok = await get().startRecording('   ');
    });

    expect(ok).toBe(false);
    expect(mockedAxios.post).not.toHaveBeenCalled();
  });

  it('stopRecording 成功后将状态置为 stopped', async () => {
    mockedAxios.post.mockResolvedValueOnce({
      data: { session_id: 'sess-1', status: 'stopped' },
    });
    mockedAxios.get.mockResolvedValueOnce({ data: [] });

    const { get } = mountHook();
    // 先启动一个 session
    mockedAxios.post.mockResolvedValueOnce({
      data: { session_id: 'sess-1', status: 'recording' },
    });
    await act(async () => {
      await get().startRecording('https://example.com');
    });

    let ok = false;
    await act(async () => {
      ok = await get().stopRecording();
    });

    expect(ok).toBe(true);
    expect(mockedAxios.post).toHaveBeenCalledWith(
      expect.objectContaining({
        method: 'POST',
        url: '/api/recordings/sess-1/stop',
      })
    );
    expect(get().status).toBe('stopped');
    expect(get().loading).toBe(false);
  });

  it('stopRecording 失败时返回 false', async () => {
    const { get } = mountHook();
    mockedAxios.post.mockResolvedValueOnce({
      data: { session_id: 'sess-2', status: 'recording' },
    });
    await act(async () => {
      await get().startRecording('https://example.com');
    });

    mockedAxios.post.mockRejectedValueOnce(new Error('boom'));
    let ok = true;
    await act(async () => {
      ok = await get().stopRecording();
    });

    expect(ok).toBe(false);
    expect(get().status).toBe('recording');
    expect(get().loading).toBe(false);
  });

  it('convertToSteps 成功时把后端返回的 steps 写入状态', async () => {
    const { get } = mountHook();
    mockedAxios.post.mockResolvedValueOnce({
      data: { session_id: 'sess-3', status: 'recording' },
    });
    await act(async () => {
      await get().startRecording('https://example.com');
    });

    const fakeSteps = [
      { step_description: '打开登录页', expected_result: '进入登录页' },
      { step_description: '输入用户名', expected_result: '用户名回显正确' },
    ];
    mockedAxios.post.mockResolvedValueOnce({ data: { steps: fakeSteps } });

    let ok = false;
    await act(async () => {
      ok = await get().convertToSteps();
    });

    expect(ok).toBe(true);
    expect(mockedAxios.post).toHaveBeenCalledWith(
      expect.objectContaining({
        method: 'POST',
        url: '/api/recordings/sess-3/convert',
        data: { session_id: 'sess-3' },
      })
    );
    expect(get().steps).toEqual(fakeSteps);
    expect(get().converting).toBe(false);
  });

  it('convertToSteps 在没有 sessionId 时直接返回 false', async () => {
    const { get } = mountHook();
    let ok = true;
    await act(async () => {
      ok = await get().convertToSteps();
    });

    expect(ok).toBe(false);
    expect(mockedAxios.post).not.toHaveBeenCalled();
    expect(get().steps).toEqual([]);
  });

  it('convertToSteps 失败时返回 false', async () => {
    const { get } = mountHook();
    mockedAxios.post.mockResolvedValueOnce({
      data: { session_id: 'sess-4', status: 'recording' },
    });
    await act(async () => {
      await get().startRecording('https://example.com');
    });

    mockedAxios.post.mockRejectedValueOnce(new Error('convert fail'));
    let ok = true;
    await act(async () => {
      ok = await get().convertToSteps();
    });

    expect(ok).toBe(false);
    expect(get().steps).toEqual([]);
    expect(get().converting).toBe(false);
  });

  it('refreshEvents 在有 session 时拉取并写入 events', async () => {
    const { get } = mountHook();
    mockedAxios.post.mockResolvedValueOnce({
      data: { session_id: 'sess-5', status: 'recording' },
    });
    await act(async () => {
      await get().startRecording('https://example.com');
    });

    mockedAxios.get.mockResolvedValueOnce({
      data: [
        { event_type: 'click', timestamp: 1700000000, selector: '#btn' },
      ],
    });

    let ok = false;
    await act(async () => {
      ok = await get().refreshEvents();
    });

    expect(ok).toBe(true);
    expect(mockedAxios.get).toHaveBeenCalledWith(
      expect.objectContaining({
        method: 'GET',
        url: '/api/recordings/sess-5/events',
      })
    );
    expect(get().events).toHaveLength(1);
    expect(get().events[0].event_type).toBe('click');
  });
});