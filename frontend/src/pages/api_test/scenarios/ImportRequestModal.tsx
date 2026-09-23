import React, { useEffect, useMemo, useState } from 'react';
import { Button, Empty, Input, Modal, Space, Spin, Table, Tabs, Tag, Tree } from '@arco-design/web-react';
import { apiGet } from '@/utils/apiRequest';
import { ApiDefinition, Module } from '../definitions/types';
import styles from './import.module.less';

export interface ApiCaseRow {
  id: number;
  name: string;
  module_id?: number | null;
  api_definition_id?: number | null;
  api_spec?: {
    steps?: Array<{
      definition_id?: number | null;
      request?: { method?: string; url?: string };
    }>;
  } | null;
}

export interface PickedCase {
  id: number;
  name: string;
  method: string;
  path: string;
  definitionName: string;
}

interface Props {
  visible: boolean;
  projectId: number | null;
  existingCaseIds: number[];
  onCancel: () => void;
  onConfirm: (definitions: ApiDefinition[], cases: PickedCase[]) => void;
}

const METHOD_TAG_COLOR: Record<string, string> = {
  GET: 'green',
  POST: 'orange',
  PUT: 'arcoblue',
  PATCH: 'purple',
  DELETE: 'red',
  HEAD: 'gray',
  OPTIONS: 'gray',
};

const buildModuleTree = (flat: Module[]): Module[] => {
  const byId = new Map<number, Module>();
  flat.forEach((m) => byId.set(m.id, { ...m, children: [] }));
  const roots: Module[] = [];
  byId.forEach((m) => {
    const parent = m.parent_id != null ? byId.get(m.parent_id) : undefined;
    if (parent) parent.children!.push(m);
    else roots.push(m);
  });
  return roots;
};

const collectIds = (node: Module): number[] => [
  node.id,
  ...(node.children || []).flatMap(collectIds),
];

const findModule = (nodes: Module[], id: number): Module | undefined => {
  for (const node of nodes) {
    if (node.id === id) return node;
    const nested = findModule(node.children || [], id);
    if (nested) return nested;
  }
  return undefined;
};

const describeCase = (item: ApiCaseRow, defs: ApiDefinition[]) => {
  const first = item.api_spec?.steps?.[0];
  const defId = item.api_definition_id ?? first?.definition_id ?? null;
  const def = defId != null ? defs.find((d) => d.id === defId) : undefined;
  const method = (def?.method || first?.request?.method || '').toUpperCase();
  const rawUrl = first?.request?.url || '';
  const path = def?.path || rawUrl.replace(/\{\{\s*baseUrl\s*\}\}/, '') || rawUrl;
  return {
    method,
    path,
    name: def?.name || '',
    moduleId: def?.module_id ?? item.module_id ?? null,
  };
};

const MethodTag = ({ method }: { method: string }) =>
  method ? <Tag color={METHOD_TAG_COLOR[method] || 'gray'}>{method}</Tag> : <span>—</span>;

const ImportRequestModal: React.FC<Props> = ({
  visible,
  projectId,
  existingCaseIds,
  onCancel,
  onConfirm,
}) => {
  const [loading, setLoading] = useState(false);
  const [tab, setTab] = useState('definition');
  const [modules, setModules] = useState<Module[]>([]);
  const [definitions, setDefinitions] = useState<ApiDefinition[]>([]);
  const [cases, setCases] = useState<ApiCaseRow[]>([]);
  const [moduleKey, setModuleKey] = useState('all');
  const [moduleQuery, setModuleQuery] = useState('');
  const [keyword, setKeyword] = useState('');
  const [pickedDefIds, setPickedDefIds] = useState<number[]>([]);
  const [pickedCaseIds, setPickedCaseIds] = useState<number[]>([]);

  const excludedKey = existingCaseIds.join(',');

  useEffect(() => {
    if (!visible || !projectId) return;
    setTab('definition');
    setModuleKey('all');
    setModuleQuery('');
    setKeyword('');
    setPickedDefIds([]);
    setPickedCaseIds([]);
    const excluded = new Set(excludedKey.split(',').filter(Boolean).map(Number));
    let cancelled = false;
    const load = async () => {
      setLoading(true);
      try {
        const [moduleData, defData, caseData] = await Promise.all([
          apiGet<Module[]>(`/api/projects/${projectId}/modules`).catch(() => []),
          apiGet<{ items: ApiDefinition[] }>('/api/api-test/definitions', {
            project_id: projectId,
            page_size: 200,
          }),
          apiGet<{ items: ApiCaseRow[] }>(`/api/testcases/project/${projectId}/testcases`, {
            case_kind: 'api',
            size: 200,
          }),
        ]);
        if (cancelled) return;
        setModules(Array.isArray(moduleData) ? moduleData : []);
        setDefinitions(defData?.items || []);
        const items = Array.isArray(caseData) ? caseData : caseData?.items || [];
        setCases(items.filter((item) => !excluded.has(item.id)));
      } finally {
        if (!cancelled) setLoading(false);
      }
    };
    load();
    return () => {
      cancelled = true;
    };
  }, [visible, projectId, excludedKey]);

  const tree = useMemo(() => buildModuleTree(modules), [modules]);
  const describedCases = useMemo(
    () => cases.map((item) => ({ item, api: describeCase(item, definitions) })),
    [cases, definitions]
  );

  const moduleIds = useMemo(() => {
    if (moduleKey === 'all' || moduleKey === 'ungrouped') return null;
    const id = Number(moduleKey.replace('module-', ''));
    const node = findModule(tree, id);
    return node ? new Set(collectIds(node)) : new Set<number>();
  }, [moduleKey, tree]);

  const inModule = (moduleId: number | null) => {
    if (moduleKey === 'all') return true;
    if (moduleKey === 'ungrouped') return moduleId == null;
    return moduleId != null && !!moduleIds?.has(moduleId);
  };

  const visibleDefs = definitions.filter((def) => {
    if (!inModule(def.module_id)) return false;
    const q = keyword.trim().toLowerCase();
    if (!q) return true;
    return `${def.name} ${def.path} ${def.method}`.toLowerCase().includes(q);
  });
  const visibleCases = describedCases.filter(({ item, api }) => {
    if (!inModule(api.moduleId)) return false;
    const q = keyword.trim().toLowerCase();
    if (!q) return true;
    return `${item.name} ${api.name} ${api.path} ${api.method}`.toLowerCase().includes(q);
  });

  const countIn = (moduleId: number | null, includeChildren = false) => {
    const ids = includeChildren && moduleId != null
      ? new Set(collectIds(findModule(tree, moduleId) || { id: moduleId, project_id: 0, name: '', parent_id: null }))
      : null;
    const match = (id: number | null) => {
      if (moduleId == null) return id == null;
      return id != null && (ids ? ids.has(id) : id === moduleId);
    };
    if (tab === 'definition') return definitions.filter((d) => match(d.module_id)).length;
    return describedCases.filter(({ api }) => match(api.moduleId)).length;
  };

  const treeData = useMemo(() => {
    const q = moduleQuery.trim().toLowerCase();
    const toNode = (node: Module): { key: string; title: string; children?: ReturnType<typeof toNode>[] } | null => {
      const children = (node.children || []).map(toNode).filter(Boolean) as ReturnType<typeof toNode>[];
      const selfMatch = !q || node.name.toLowerCase().includes(q);
      if (!selfMatch && children.length === 0) return null;
      return {
        key: `module-${node.id}`,
        title: `${node.name} (${countIn(node.id, true)})`,
        children,
      };
    };
    const moduleNodes = tree.map(toNode).filter(Boolean);
    return [
      { key: 'all', title: `${tab === 'definition' ? '全部接口' : '全部用例'} (${tab === 'definition' ? definitions.length : describedCases.length})` },
      { key: 'ungrouped', title: `未分组 (${countIn(null)})` },
      ...moduleNodes,
    ];
  }, [tree, tab, definitions, describedCases, moduleQuery]);

  const confirm = () => {
    const pickedDefs = definitions.filter((d) => pickedDefIds.includes(d.id));
    const pickedCases = describedCases
      .filter(({ item }) => pickedCaseIds.includes(item.id))
      .map(({ item, api }) => ({
        id: item.id,
        name: item.name,
        method: api.method,
        path: api.path,
        definitionName: api.name,
      }));
    onConfirm(pickedDefs, pickedCases);
  };

  return (
    <Modal
      title="导入请求"
      visible={visible}
      onCancel={onCancel}
      style={{ width: 980 }}
      unmountOnExit
      footer={
        <div className={styles.footer}>
          <span className={styles.count}>
            共选择 {pickedDefIds.length} 个接口、{pickedCaseIds.length} 条用例
          </span>
          <Space>
            <Button onClick={onCancel}>取消</Button>
            <Button type="primary" onClick={confirm} disabled={!pickedDefIds.length && !pickedCaseIds.length}>
              引用
            </Button>
          </Space>
        </div>
      }
    >
      <Tabs activeTab={tab} onChange={setTab}>
        <Tabs.TabPane key="definition" title="接口" />
        <Tabs.TabPane key="case" title="用例" />
      </Tabs>
      <Spin loading={loading} style={{ width: '100%' }}>
        <div className={styles.body}>
          <div className={styles.treePane}>
            <Input
              allowClear
              placeholder="输入模块名称搜索"
              value={moduleQuery}
              onChange={setModuleQuery}
            />
            <Tree
              blockNode
              selectedKeys={[moduleKey]}
              treeData={treeData}
              onSelect={(keys) => setModuleKey(String(keys[0] || 'all'))}
            />
          </div>
          <div className={styles.tablePane}>
            <Input
              allowClear
              placeholder="输入名称或路径搜索"
              value={keyword}
              onChange={setKeyword}
            />
            {tab === 'definition' ? (
              visibleDefs.length === 0 ? (
                <Empty description="该分组下没有接口" />
              ) : (
                <Table
                  size="small"
                  rowKey="id"
                  data={visibleDefs}
                  pagination={{ pageSize: 10 }}
                  rowSelection={{
                    selectedRowKeys: pickedDefIds,
                    preserveSelectedRowKeys: true,
                    onChange: (keys) => setPickedDefIds(keys as number[]),
                  }}
                  columns={[
                    { title: 'ID', dataIndex: 'id', width: 80 },
                    { title: '接口名称', dataIndex: 'name' },
                    {
                      title: '请求类型',
                      dataIndex: 'method',
                      width: 100,
                      render: (method: string) => <MethodTag method={(method || '').toUpperCase()} />,
                    },
                    { title: '路径', dataIndex: 'path' },
                  ]}
                />
              )
            ) : visibleCases.length === 0 ? (
              <Empty description="该分组下没有用例" />
            ) : (
              <Table
                size="small"
                rowKey={(row) => row.item.id}
                data={visibleCases}
                pagination={{ pageSize: 10 }}
                rowSelection={{
                  selectedRowKeys: pickedCaseIds,
                  preserveSelectedRowKeys: true,
                  onChange: (keys) => setPickedCaseIds(keys as number[]),
                }}
                columns={[
                  { title: 'ID', width: 80, render: (_: unknown, row) => row.item.id },
                  { title: '用例名称', render: (_: unknown, row) => row.item.name },
                  {
                    title: '请求类型',
                    width: 100,
                    render: (_: unknown, row) => <MethodTag method={row.api.method} />,
                  },
                  { title: '路径', render: (_: unknown, row) => row.api.path || '—' },
                  { title: '接口名称', render: (_: unknown, row) => row.api.name || '—' },
                ]}
              />
            )}
          </div>
        </div>
      </Spin>
    </Modal>
  );
};

export default ImportRequestModal;
