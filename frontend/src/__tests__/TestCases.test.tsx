import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, waitFor } from '@testing-library/react';
import { BrowserRouter } from 'react-router-dom';
import axios from 'axios';
import TestCases from '@/pages/testcases';

/* Vitest 0.10.5 没有 vi.hoisted；把 mock 句柄挂在 mock 实例自身上，
 * 测试代码通过 vi.mocked(axios) 取回。 */
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

const mockedAxios = vi.mocked(axios as any);
const m = (mockedAxios as any).__mocks as {
  axiosGet: ReturnType<typeof vi.fn>;
  axiosPost: ReturnType<typeof vi.fn>;
  axiosPut: ReturnType<typeof vi.fn>;
  axiosDelete: ReturnType<typeof vi.fn>;
  axiosDefault: ReturnType<typeof vi.fn>;
};

const allCalledUrls = (): string[] => [
  ...m.axiosDefault.mock.calls.map((c: any[]) => String(c[0]?.url || '')),
  ...m.axiosGet.mock.calls.map((c: any[]) => String(c[0] || '')),
  ...m.axiosPost.mock.calls.map((c: any[]) => String(c[0] || '')),
];

vi.mock('@arco-design/web-react/icon', () => ({
  IconEdit: () => null,
  IconDelete: () => null,
  IconPlayArrow: () => null,
  IconStar: () => null,
  IconStarFill: () => null,
  IconBug: () => null,
  IconFolder: () => null,
  IconSettings: () => null,
  IconPlus: () => null,
  IconFolderAdd: () => null,
}));

vi.mock('@/pages/testcases/style/components.module.less', () => {
  return new Proxy({}, { get: (_, key) => String(key) });
});

vi.mock('@/utils/useLocale', () => ({
  default: () => ({
    'select.project': '选择项目',
    'select.module': '选择模块',
    'select.agent': '选择 Agent',
    'search.placeholder': '搜索用例',
    'search': '搜索',
    'clear': '清空',
    'create.case': '新建用例',
    'create.module': '新建模块',
    'batch.run.server': '服务端批量运行',
    'batch.run.client': 'Agent 批量运行',
    'batch.move': '批量移动',
    'batch.copy': '批量复制',
    'delete.batch': '批量删除 {count}',
    'confirm.delete': '确认删除',
    'confirm.delete.item': '确认删除该用例？',
    'confirm.delete.module': '确认删除模块 {name}？',
    'cannot.delete.module': '该模块下仍有用例，无法删除',
    'delete.failed': '删除失败',
    'init.case': '初始化用例',
    'init.case.mark': '已标记为初始化用例',
    'init.case.unmark': '已取消初始化',
    'init.case.run_before': '先执行初始化用例',
    'init.case.select': '选择初始化用例',
    'init.case.none': '当前项目没有初始化用例',
    'name': '名称',
    'module': '模块',
    'description': '描述',
    'actions': '操作',
    'run': '运行',
    'client': 'Agent',
    'operate.failed': '操作失败',
    'update.success': '更新成功',
    'create.success': '创建成功',
    'deleted': '已删除',
    'run.triggered': '已触发运行',
    'client.run.triggered': '已分发到 Agent: {agent}',
    'run.failed': '运行失败',
    'case.count': '已选 {count} 个用例',
    'environment.create_success': '环境创建成功',
    'environment.update_success': '环境更新成功',
    'environment.delete_success': '环境删除成功',
    'environment.set_default_success': '已设为默认环境',
  }),
}));

const renderWithRouter = (ui: React.ReactElement) =>
  render(<BrowserRouter>{ui}</BrowserRouter>);

describe('TestCases', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    const emptyResolved = Promise.resolve({ data: [] } as any);
    const emptyPaged = Promise.resolve({ data: { items: [], data: [], total_items: 0 } } as any);
    const emptyObj = Promise.resolve({ data: {} } as any);
    m.axiosDefault.mockImplementation((cfg: any) => {
      const url = String(cfg?.url || '');
      if (url.includes('/api/projects/')) return emptyResolved;
      return emptyPaged;
    });
    m.axiosGet.mockImplementation((url: string) => {
      if (String(url).includes('/api/projects/')) return emptyResolved;
      return emptyPaged;
    });
    m.axiosPost.mockResolvedValue(emptyObj);
    m.axiosPut.mockResolvedValue(emptyObj);
    m.axiosDelete.mockResolvedValue(emptyObj);
  });

  it('渲染时不崩溃', () => {
    expect(() => renderWithRouter(<TestCases />)).not.toThrow();
  });

  it('触发项目列表查询 /api/projects/', async () => {
    renderWithRouter(<TestCases />);
    await waitFor(() => {
      const urls = allCalledUrls();
      expect(urls.some((u) => u.includes('/api/projects/'))).toBe(true);
    });
  });

  it('触发 agent 列表查询 /api/agents', async () => {
    renderWithRouter(<TestCases />);
    await waitFor(() => {
      const urls = allCalledUrls();
      expect(urls.some((u) => u.includes('/api/agents'))).toBe(true);
    });
  });

  it('未选项目时不触发 testcases 列表查询', async () => {
    renderWithRouter(<TestCases />);
    await new Promise((resolve) => setTimeout(resolve, 50));
    const urls = allCalledUrls();
    expect(urls.some((u) => u.includes('/api/testcases/'))).toBe(false);
  });

  it('组件挂载后根节点存在', () => {
    const { container } = renderWithRouter(<TestCases />);
    expect(container.firstChild).not.toBeNull();
  });
});
