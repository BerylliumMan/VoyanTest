import { describe, expect, it } from 'vitest';
import { renderEvidenceLinks } from '../utils/evidenceLinks';

/**
 * 036：fetch 存证（.csv/.json 等）必须可点击——证据链接正则扩展回归。
 */
describe('evidenceLinks：证据路径链接渲染', () => {
  it('csv 存证路径渲染为链接', () => {
    const node = renderEvidenceLinks(
      '页面 API POST /api/export → 200（以 API 方式完成） · 存证 reports/agent_1_20261016/evidence/export.csv',
    );
    const arr = node as unknown as Array<unknown>;
    expect(Array.isArray(arr)).toBe(true);
    expect(JSON.stringify(arr)).toContain('/reports/agent_1_20261016/evidence/export.csv');
  });

  it('json/txt/xlsx 同样可点击', () => {
    for (const ext of ['json', 'txt', 'xlsx', 'webm', 'pdf']) {
      const node = renderEvidenceLinks(`存证 reports/x/evidence/a.${ext}`);
      expect(JSON.stringify(node)).toContain(`/reports/x/evidence/a.${ext}`);
    }
  });

  it('无证据路径时原样返回文本', () => {
    expect(renderEvidenceLinks('普通步骤文本')).toBe('普通步骤文本');
  });

  it('非白名单扩展名不渲染为链接', () => {
    const node = renderEvidenceLinks('存证 reports/x/evidence/a.exe');
    expect(typeof node).toBe('string');
  });
});
