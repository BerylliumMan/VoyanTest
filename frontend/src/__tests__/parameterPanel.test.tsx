import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import ParameterPanel from '@/pages/testcases/components/ParameterPanel';
import { Step } from '@/pages/testcases/types';

vi.mock('axios', () => ({ default: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() } }));

const steps: Step[] = [{ step_order: 1, description: '输入 {{username}} 和 [[password]]' }];

const baseProps = {
  steps,
  parameters: [],
  onParametersChange: vi.fn(),
  datasets: [{ id: 1, name: 'UI 数据', columns: ['username'], rows: [{ username: 'admin' }] }],
  binding: {
    dataset_id: 1,
    dataset_mode: 'sequential' as const,
    loop_count: 1,
    fail_fast: false,
  },
  onBindingChange: vi.fn(),
  t: {},
};

describe('ParameterPanel', () => {
  it('自动发现步骤中的 {{参数}}', () => {
    render(<ParameterPanel {...baseProps} />);
    expect(screen.getAllByText(/username/).length).toBeGreaterThan(0);
  });

  it('循环模式显示组数（行数 × 循环次数）', () => {
    render(
      <ParameterPanel
        {...baseProps}
        binding={{ ...baseProps.binding, dataset_mode: 'loop', loop_count: 3 }}
      />
    );
    expect(screen.getByText(/本次执行 3 组/)).toBeTruthy();
  });
});
