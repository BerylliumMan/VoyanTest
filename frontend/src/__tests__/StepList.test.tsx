import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import StepList from '@/pages/testcases/components/StepList';
import { Step } from '@/pages/testcases/types';

/* 模拟 CSS Module 导入 */
vi.mock('@/pages/testcases/style/components.module.less', () => ({}));

/* 模拟 axios – 确保测试不触发真实 HTTP 请求 */
vi.mock('axios', () => ({
  default: {
    get: vi.fn(),
    post: vi.fn(),
    put: vi.fn(),
    delete: vi.fn(),
  },
}));

const noop = (): (() => void) => () => {};

interface MakePropsOptions {
  steps?: Step[];
  copiedStep?: Step | null;
}
const makeProps = (opts?: MakePropsOptions) => {
  const steps: Step[] = opts?.steps ?? [
    { step_order: 0, description: '打开登录页面' },
    { step_order: 1, description: '输入用户名和密码' },
    { step_order: 2, description: '点击登录按钮' },
  ];
  return {
    steps,
    onAdd: vi.fn(),
    onRemove: vi.fn(),
    onUpdate: vi.fn(),
    onInsert: vi.fn(),
    onCopy: vi.fn(),
    onPaste: vi.fn(),
    copiedStep: opts?.copiedStep ?? null,
    onDragStart: vi.fn().mockReturnValue(noop),
    onDragOver: vi.fn().mockReturnValue(noop),
    onDragLeave: vi.fn().mockReturnValue(noop),
    onDrop: vi.fn().mockReturnValue(noop),
    t: {
      'step.placeholder': '用自然语言描述测试步骤',
      'step.result.placeholder': '预期结果',
      'step.insert_above': '在上方插入步骤',
      'step.copy': '复制步骤',
      'step.paste': '粘贴步骤',
      'add.step': '添加步骤',
    } as Record<string, string>,
  };
};

describe('StepList', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  /* --- 冒烟测试 --- */

  it('渲染时不崩溃', () => {
    const props = makeProps();
    render(<StepList {...props} />);
    expect(screen.getByText('添加步骤')).toBeInTheDocument();
  });

  it('显示正确的步骤数量', () => {
    const props = makeProps();
    render(<StepList {...props} />);
    /* 每个步骤行包含一个序号 Tag（1, 2, 3），此外还有一个 "添加步骤" 按钮 */
    expect(screen.getByText('1')).toBeInTheDocument();
    expect(screen.getByText('2')).toBeInTheDocument();
    expect(screen.getByText('3')).toBeInTheDocument();
  });

  it('显示每个步骤的描述文本', () => {
    const props = makeProps();
    render(<StepList {...props} />);
    const textareas = screen.getAllByPlaceholderText('用自然语言描述测试步骤');
    expect(textareas).toHaveLength(3);
    expect(textareas[0]).toHaveValue('打开登录页面');
    expect(textareas[1]).toHaveValue('输入用户名和密码');
    expect(textareas[2]).toHaveValue('点击登录按钮');
  });

  it('点击「添加步骤」触发 onAdd', () => {
    const props = makeProps();
    render(<StepList {...props} />);
    fireEvent.click(screen.getByText('添加步骤'));
    expect(props.onAdd).toHaveBeenCalledTimes(1);
  });

  it('点击删除按钮触发 onRemove 并传入正确索引', () => {
    const props = makeProps();
    render(<StepList {...props} />);
    const deleteButtons = screen.getAllByLabelText('删除步骤');
    expect(deleteButtons).toHaveLength(3);
    fireEvent.click(deleteButtons[1]); // 删除第二个步骤（索引 1）
    expect(props.onRemove).toHaveBeenCalledWith(1);
  });

  it('当有复制步骤时显示粘贴按钮', () => {
    const copiedStep: Step = { step_order: 0, description: '测试步骤' };
    const props = makeProps({ copiedStep });
    render(<StepList {...props} />);
    const pasteButtons = screen.getAllByLabelText('粘贴步骤');
    expect(pasteButtons.length).toBeGreaterThan(0);
  });
});
