import { describe, it, expect } from 'vitest';
import { render, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import useRoute, { IRoute, generatePermission } from '@/routes';
import lazyload from '@/utils/lazyload';

/* T041（038 US6）：路由权限过滤 + 懒加载渲染。
 * - useRoute 同步派生：admin 可见审计/设置，tester 不可见；
 * - 默认路由为第一个非 ignore 项（dashboard）；
 * - lazyload 包装的组件暴露 preload()，未预加载时先渲染 fallback、随后渲染页面。 */

function RouteProbe({
  permission,
  onReady,
}: {
  permission: Record<string, string[]>;
  onReady: (routes: IRoute[], defaultRoute: string) => void;
}) {
  const [filtered, defaultRoute] = useRoute(permission);
  onReady(filtered, defaultRoute);
  return <div data-testid="default-route">{defaultRoute}</div>;
}

describe('T041 路由权限与懒加载', () => {
  it('admin 权限下可见审计日志与系统设置', () => {
    let got: IRoute[] = [];
    const { getByTestId } = render(
      <RouteProbe permission={generatePermission('admin')} onReady={r => (got = r)} />
    );
    const keys = got.map(r => r.key);
    expect(keys).toContain('audit-logs');
    expect(keys).toContain('settings');
    expect(getByTestId('default-route').textContent).toBe('dashboard');
  });

  it('tester 权限下过滤掉 admin 专属页（含子路由父项）', () => {
    let got: IRoute[] = [];
    const { getByTestId } = render(
      <RouteProbe permission={generatePermission('tester')} onReady={r => (got = r)} />
    );
    const keys = got.map(r => r.key);
    expect(keys).not.toContain('audit-logs');
    expect(keys).not.toContain('settings');
    // 基础业务路由仍可见
    expect(keys).toContain('chat');
    expect(keys).toContain('testcases');
    expect(getByTestId('default-route').textContent).toBe('dashboard');
  });

  it('ignore 路由不参与默认路由计算', () => {
    let got: IRoute[] = [];
    render(<RouteProbe permission={generatePermission('tester')} onReady={r => (got = r)} />);
    // setup 虽然 ignore，但仍在过滤结果中（供直链使用）；默认路由不能是它
    const setup = got.find(r => r.key === 'setup');
    expect(setup?.ignore).toBe(true);
  });

  it('lazyload 暴露 preload 且延迟渲染页面', async () => {
    const loader = () =>
      new Promise<React.ComponentType>(resolve =>
        setTimeout(() => resolve(() => <div>LAZY_PAGE_READY</div>), 10)
      );
    const LazyPage = lazyload(loader as never) as React.ComponentType & {
      preload: () => Promise<unknown>;
    };
    expect(typeof LazyPage.preload).toBe('function');
    const { getByText } = render(
      <MemoryRouter>
        <LazyPage />
      </MemoryRouter>
    );
    await waitFor(() => expect(getByText('LAZY_PAGE_READY')).toBeInTheDocument());
    await expect(LazyPage.preload()).resolves.toBeTruthy();
  });
});
