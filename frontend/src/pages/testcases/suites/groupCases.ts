export interface GroupCase {
  id: number;
  name: string;
  module_id?: number | null;
}

export interface GroupModule {
  id: number;
  name: string;
  parent_id?: number | null;
}

export interface ModuleNode<C extends GroupCase = GroupCase> {
  key: string;
  id: number | null;
  name: string;
  level: number;
  /** 当前模块直属、且命中搜索的用例 */
  cases: C[];
  /** 子模块（同样已按搜索裁剪） */
  children: ModuleNode<C>[];
  /** 当前模块整棵子树（含直属与后代）的全部用例，不受搜索影响 */
  allCases: C[];
}

/** 收集节点整棵子树中的用例 id（含自身与后代） */
export function collectNodeCaseIds<C extends GroupCase>(node: ModuleNode<C>): number[] {
  return node.allCases.map((c) => c.id);
}

/**
 * 按模块层级构建用例树：
 * - 模块顺序跟随后端返回顺序；父模块缺失时按根处理
 * - 搜索只裁剪用例；若某子模块有命中，其祖先模块仍保留
 * - 未分类用例挂在根级末尾
 */
export function buildModuleCaseTree<C extends GroupCase>(
  cases: C[],
  modules: GroupModule[],
  query: string,
  ungroupedLabel: string,
): ModuleNode<C>[] {
  const q = (query || '').trim().toLowerCase();
  const matches = (c: C) => !q || (c.name || '').toLowerCase().includes(q);

  const moduleIds = new Set(modules.map((m) => m.id));
  const childrenOf = new Map<number | null, GroupModule[]>();
  for (const m of modules) {
    const parent = m.parent_id != null && moduleIds.has(m.parent_id) ? m.parent_id : null;
    const list = childrenOf.get(parent) || [];
    list.push(m);
    childrenOf.set(parent, list);
  }

  const directByModule = new Map<number, C[]>();
  const ungroupedAll: C[] = [];
  const caseById = new Map<number, C>();
  for (const c of cases) {
    caseById.set(c.id, c);
    if (c.module_id == null || !moduleIds.has(c.module_id)) {
      ungroupedAll.push(c);
    } else {
      const list = directByModule.get(c.module_id) || [];
      list.push(c);
      directByModule.set(c.module_id, list);
    }
  }

  const build = (m: GroupModule, level: number, seen: Set<number>): ModuleNode<C> | null => {
    if (seen.has(m.id)) return null; // 防御环状 parent_id
    seen.add(m.id);
    const directAll = directByModule.get(m.id) || [];
    const children: ModuleNode<C>[] = [];
    for (const child of childrenOf.get(m.id) || []) {
      const node = build(child, level + 1, new Set(seen));
      if (node) children.push(node);
    }
    const directMatched = directAll.filter(matches);
    const keptChildren = q ? children.filter((n) => n.cases.length > 0 || n.children.length > 0) : children;
    if (q && directMatched.length === 0 && keptChildren.length === 0) return null;
    return {
      key: `m${m.id}`,
      id: m.id,
      name: m.name,
      level,
      cases: directMatched,
      children: keptChildren,
      allCases: collectSubtreeCases(m.id, new Set()),
    };
  };

  // 整棵子树用例（不受搜索影响，用于模块勾选）
  const subtreeMemo = new Map<number, C[]>();
  const collectSubtreeCases = (id: number, stack: Set<number>): C[] => {
    if (subtreeMemo.has(id)) return subtreeMemo.get(id) as C[];
    if (stack.has(id)) return [];
    stack.add(id);
    const direct = directByModule.get(id) || [];
    const all = [...direct];
    for (const child of childrenOf.get(id) || []) {
      all.push(...collectSubtreeCases(child.id, new Set(stack)));
    }
    subtreeMemo.set(id, all);
    return all;
  };

  const roots: ModuleNode<C>[] = [];
  for (const m of childrenOf.get(null) || []) {
    const node = build(m, 0, new Set());
    if (node) roots.push(node);
  }

  const ungroupedMatched = ungroupedAll.filter(matches);
  if (!q || ungroupedMatched.length > 0) {
    roots.push({
      key: 'ungrouped',
      id: null,
      name: ungroupedLabel,
      level: 0,
      cases: ungroupedMatched,
      children: [],
      allCases: ungroupedAll,
    });
  }
  return roots;
}

/** 兼容旧调用：返回扁平分组（不保留层级） */
export function groupCasesByModule<C extends GroupCase>(
  cases: C[],
  modules: GroupModule[],
  query: string,
  ungroupedLabel: string,
): Array<{ key: string; name: string; cases: C[] }> {
  const out: Array<{ key: string; name: string; cases: C[] }> = [];
  const walk = (nodes: ModuleNode<C>[]) => {
    for (const n of nodes) {
      if (n.cases.length) out.push({ key: n.key, name: n.name, cases: n.cases });
      walk(n.children);
    }
  };
  walk(buildModuleCaseTree(cases, modules, query, ungroupedLabel));
  return out;
}
