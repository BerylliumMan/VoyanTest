import { describe, expect, it } from 'vitest';
import { buildModuleCaseTree, collectNodeCaseIds, groupCasesByModule } from '../pages/testcases/suites/groupCases';

const modules = [
  { id: 2, name: '仪表盘', parent_id: null },
  { id: 1, name: '登录', parent_id: null },
  { id: 5, name: '登录-子模块', parent_id: 1 },
  { id: 6, name: '登录-孙模块', parent_id: 5 },
];

const cases = [
  { id: 11, name: '登录成功', module_id: 1 },
  { id: 12, name: '登录失败', module_id: 1 },
  { id: 21, name: '统计卡片', module_id: 2 },
  { id: 31, name: '未分类用例', module_id: null },
  { id: 32, name: '孤儿模块用例', module_id: 9 },
  { id: 51, name: '子模块用例', module_id: 5 },
  { id: 61, name: '深层校验用例', module_id: 6 },
];

describe('buildModuleCaseTree', () => {
  it('按模块树构建，多级层级正确', () => {
    const roots = buildModuleCaseTree(cases, modules, '', '未分类');
    expect(roots.map((n) => n.name)).toEqual(['仪表盘', '登录', '未分类']);
    const login = roots[1];
    expect(login.children.map((n) => n.name)).toEqual(['登录-子模块']);
    expect(login.children[0].children.map((n) => n.name)).toEqual(['登录-孙模块']);
    expect(login.children[0].level).toBe(1);
    expect(login.children[0].children[0].level).toBe(2);
  });

  it('父模块 allCases 汇总整棵子树的用例（用于模块勾选）', () => {
    const roots = buildModuleCaseTree(cases, modules, '', '未分类');
    expect(collectNodeCaseIds(roots[1])).toEqual([11, 12, 51, 61]);
    expect(collectNodeCaseIds(roots[1].children[0])).toEqual([51, 61]);
  });

  it('搜索只裁剪用例，保留命中用例的祖先路径', () => {
    const roots = buildModuleCaseTree(cases, modules, '深层', '未分类');
    expect(roots.map((n) => n.name)).toEqual(['登录']);
    expect(roots[0].cases).toEqual([]);
    expect(roots[0].children[0].children[0].cases.map((c) => c.id)).toEqual([61]);
    // 搜索不影响模块勾选的汇总集合
    expect(collectNodeCaseIds(roots[0])).toEqual([11, 12, 51, 61]);
  });

  it('未分类与孤儿模块归入未分类组', () => {
    const roots = buildModuleCaseTree(cases, modules, '', '未分类');
    expect(roots[2].cases.map((c) => c.id)).toEqual([31, 32]);
  });

  it('无匹配时返回空树', () => {
    expect(buildModuleCaseTree(cases, modules, '不存在', '未分类')).toEqual([]);
  });

  it('兼容函数返回扁平分组', () => {
    const groups = groupCasesByModule(cases, modules, '', '未分类');
    expect(groups.map((g) => g.name)).toEqual([
      '仪表盘',
      '登录',
      '登录-子模块',
      '登录-孙模块',
      '未分类',
    ]);
  });
});
