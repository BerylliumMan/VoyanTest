import { describe, expect, it } from 'vitest';
import {
  deriveStructuredFromDescription,
  parseInstantToStructured,
  structuredStepIsComplete,
} from '../pages/testcases/utils/parseInstantStep';

describe('描述优先编辑：新增动作解析（上传/拖拽/弹窗/双击/右键）', () => {
  it('上传文件 路径 → upload + value，完整', () => {
    const r = parseInstantToStructured('上传文件 /tmp/a.png');
    expect(r?.action).toBe('upload');
    expect(r?.value).toBe('/tmp/a.png');
    expect(structuredStepIsComplete(r)).toBe(true);
  });

  it('在【附件上传】上传文件 路径 → 带目标', () => {
    const r = parseInstantToStructured('在【附件上传】上传文件 /tmp/a.png');
    expect(r?.action).toBe('upload');
    expect(r?.target_name).toBe('附件上传');
    expect(r?.value).toBe('/tmp/a.png');
  });

  it('中文分隔多文件 → 归一为逗号', () => {
    const r = parseInstantToStructured('上传文件 /tmp/a.png、/tmp/b.png');
    expect(r?.value).toBe('/tmp/a.png,/tmp/b.png');
  });

  it('上传头像文件（无路径）→ 保留 upload 意图但不完整', () => {
    const r = parseInstantToStructured('上传头像文件');
    expect(r?.action).toBe('upload');
    expect(r?.value).toBeFalsy();
    expect(structuredStepIsComplete(r)).toBe(false);
  });

  it('拖拽文件 路径 到【X】→ drop', () => {
    const r = parseInstantToStructured('拖拽文件 /tmp/a.png 到【上传区】');
    expect(r?.action).toBe('drop');
    expect(r?.target_name).toBe('上传区');
    expect(r?.value).toBe('/tmp/a.png');
    expect(structuredStepIsComplete(r)).toBe(true);
  });

  it('接受弹窗 / 拒绝弹窗 → dialog', () => {
    expect(parseInstantToStructured('接受弹窗')?.value).toBe('accept');
    expect(parseInstantToStructured('接受弹窗：确认删除')?.value).toBe('accept:确认删除');
    expect(parseInstantToStructured('拒绝弹窗')?.value).toBe('dismiss');
  });

  it('双击【X】/ 右键点击【X】', () => {
    const d = parseInstantToStructured('双击【商品卡片】');
    expect(d?.action).toBe('double_click');
    expect(d?.target_name).toBe('商品卡片');
    const r = parseInstantToStructured('右键点击【行记录】');
    expect(r?.action).toBe('right_click');
  });

  it('选择…文件 仍按 select 解析（不误伤）', () => {
    const r = parseInstantToStructured('选择文件类型');
    expect(r).toBeNull();
  });
});

describe('deriveStructuredFromDescription（描述改写派生，严格模式）', () => {
  it('同动作同目标改值：保留固化 selector', () => {
    const prev = { action: 'fill', target_name: 'Username', value: 'standard_user', selector: '#user-name' };
    const next = deriveStructuredFromDescription('在【Username】输入 standard_user', prev);
    expect(next?.action).toBe('fill');
    expect(next?.value).toBe('standard_user');
    expect(next?.selector).toBe('#user-name');
  });

  it('目标变化：丢弃固化 selector', () => {
    const prev = { action: 'fill', target_name: 'Username', value: 'standard_user', selector: '#user-name' };
    const next = deriveStructuredFromDescription('在【Password】输入 secret_sauce', prev);
    expect(next?.action).toBe('fill');
    expect(next?.target_name).toBe('Password');
    expect(next?.selector).toBeUndefined();
  });

  it('无法解析：返回 null（交给引擎按描述执行，绝不伪造动作）', () => {
    const prev = { action: 'click', target_name: '登录', selector: '#login' };
    expect(deriveStructuredFromDescription('确认订单已经创建成功', prev)).toBeNull();
    expect(deriveStructuredFromDescription('', prev)).toBeNull();
  });

  it('上传改路径：同目标保留 selector', () => {
    const prev = { action: 'upload', target_name: '文件', value: '/tmp/a.png', selector: 'input[type="file"]' };
    const next = deriveStructuredFromDescription('上传文件 /tmp/b.png', prev);
    expect(next?.value).toBe('/tmp/b.png');
    expect(next?.selector).toBe('input[type="file"]');
  });
});
