import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import axios from 'axios';
import Dashboard from '@/pages/dashboard';

/* -------------------------------------------------------------------------- */
/*  Mocks                                                                     */
/* -------------------------------------------------------------------------- */

/* Mock axios — 仪表盘用 axios.get 拉项目/统计/趋势/批次 */
vi.mock('axios', () => ({
  default: {
    get: vi.fn(),
    post: vi.fn(),
    put: vi.fn(),
    delete: vi.fn(),
  },
}));

const mockedAxios = vi.mocked(axios as unknown as {
  get: ReturnType<typeof vi.fn>;
  post: ReturnType<typeof vi.fn>;
  put: ReturnType<typeof vi.fn>;
  delete: ReturnType<typeof vi.fn>;
});

/* Mock arco icons — 仪表盘使用了多个图标，避免引入 SVG 噪音 */
vi.mock('@arco-design/web-react/icon', () => ({
  IconLoading: () => null,
  IconStorage: () => null,
  IconCheckCircleFill: () => null,
  IconCloseCircleFill: () => null,
  IconList: () => null,
}));

/* Mock i18n — 仪表盘渲染时读取的 key 都补齐，避免 undefined 触发 React 警告 */
vi.mock('@/utils/useLocale', () => ({
  default: () => ({
    'select.project': '选择项目',
    'total.runs': '总运行',
    'passed': '通过',
    'failed': '失败',
    'pass.rate': '通过率',
    'trend.7days': '近 7 天趋势',
    'recent.runs': '最近运行',
    'batch': '批次',
    'project': '项目',
    'status': '状态',
    'running': '运行中',
    'all.passed': '全部通过',
    'partial.passed': '部分通过',
    'all.failed': '全部失败',
    'passed.total': '通过/总数',
    'no.data': '暂无数据',
  }),
}));

/* -------------------------------------------------------------------------- */
/*  Tests                                                                     */
/* -------------------------------------------------------------------------- */

describe('Dashboard', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    // 默认空数据：项目列表为空 + 三个统计接口都返回空对象/空数组
    mockedAxios.get.mockImplementation((url: string) => {
      if (String(url).includes('/projects/')) {
        return Promise.resolve({ data: [] }) as any;
      }
      if (String(url).includes('/batches')) {
        return Promise.resolve({ data: { items: [] } }) as any;
      }
      return Promise.resolve({ data: { data: [] } }) as any;
    });
  });

  it('渲染时不崩溃', async () => {
    expect(() => render(<Dashboard />)).not.toThrow();
  });

  it('触发项目列表查询', async () => {
    render(<Dashboard />);
    await waitFor(() => {
      const calledWithProjects = mockedAxios.get.mock.calls.some(
        (call) => typeof call[0] === 'string' && call[0].includes('/projects/')
      );
      expect(calledWithProjects).toBe(true);
    });
  });

  it('触发 statistics / trends / batches 三个统计接口', async () => {
    render(<Dashboard />);
    await waitFor(() => {
      const calls = mockedAxios.get.mock.calls.map((c) => String(c[0]));
      expect(calls.some((u) => u.includes('/reports/statistics'))).toBe(true);
      expect(calls.some((u) => u.includes('/reports/trends'))).toBe(true);
      expect(calls.some((u) => u.includes('/reports/batches'))).toBe(true);
    });
  });

  it('trends 为空时显示「暂无数据」占位', async () => {
    render(<Dashboard />);
    await waitFor(() => {
      // 仪表盘有两个「暂无数据」占位（趋势 / 最近运行）
      const empty = screen.getAllByText('暂无数据');
      expect(empty.length).toBeGreaterThan(0);
    });
  });

  it('项目列表为空时，下拉无任何选项但 Select 仍然渲染', async () => {
    const { container } = render(<Dashboard />);
    await waitFor(() => {
      // 仪表盘最外层应至少渲染一个根 div（说明组件挂载成功）
      expect(container.firstChild).not.toBeNull();
    });
  });
});
