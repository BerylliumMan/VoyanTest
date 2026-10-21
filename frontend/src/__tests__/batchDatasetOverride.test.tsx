import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import ParameterPanel from '@/pages/testcases/components/ParameterPanel';

vi.mock('axios', () => ({ default: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() } }));

const props = {
  steps: [{ step_order: 1, description: '搜索 {{keyword}}' }],
  parameters: [{ name: 'keyword' }],
  onParametersChange: vi.fn(),
  datasets: [{ id: 2, name: '批量数据', columns: ['keyword'], rows: [{ keyword: 'a' }, { keyword: 'b' }] }],
  binding: {
    dataset_id: 2,
    dataset_mode: 'loop' as const,
    loop_count: 2,
    fail_fast: true,
  },
  onBindingChange: vi.fn(),
  t: {},
};

describe('batch dataset override', () => {
  it('按覆盖的循环次数计算组数', () => {
    render(<ParameterPanel {...props} />);
    expect(screen.getByText(/本次执行 4 组/)).toBeTruthy();
  });
});
