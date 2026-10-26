import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { useHistory } from 'react-router-dom';
import axios from 'axios';
import {
  Button,
  Card,
  Input,
  Message,
  Modal,
  Select,
  Space,
  Table,
  Tag,
  Tree,
  Typography,
} from '@arco-design/web-react';
import {
  IconDelete,
  IconEdit,
  IconPlayArrow,
  IconPlus,
  IconImport,
} from '@arco-design/web-react/icon';
import useTestCaseData from '../../testcases/hooks/useTestCaseData';

const { Text } = Typography;

interface Project {
  id: number;
  name: string;
}

interface Module {
  id: number;
  name: string;
  parent_id: number | null;
  children?: Module[];
}

interface ApiDefinition {
  id: number;
  name: string;
  method: string;
  path: string;
}

const stepSummary = (record: any): string => {
  const steps = record?.api_spec?.steps || [];
  if (!steps.length) return '—';
  if (steps.length > 1) return `${steps.length} 个请求`;
  const req = steps[0]?.request || {};
  return `${req.method || 'GET'} ${req.url || ''}`;
};

const toTreeData = (nodes: Module[]): any[] =>
  (nodes || []).map((node) => ({
    key: String(node.id),
    title: node.name,
    children: toTreeData(node.children || []),
  }));

const ApiCasesPage: React.FC = () => {
  const history = useHistory();
  const [projects, setProjects] = useState<Project[]>([]);
  const [projectId, setProjectId] = useState<number | null>(null);
  const [selectedModuleId, setSelectedModuleId] = useState<number | null>(null);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);
  const [searchInput, setSearchInput] = useState('');
  const [submittedQuery, setSubmittedQuery] = useState('');
  const [selectedRowKeys, setSelectedRowKeys] = useState<number[]>([]);
  const [defModalVisible, setDefModalVisible] = useState(false);
  const [definitions, setDefinitions] = useState<ApiDefinition[]>([]);
  const [selectedDefs, setSelectedDefs] = useState<number[]>([]);
  const [defLoading, setDefLoading] = useState(false);
  const [lastRuns, setLastRuns] = useState<Record<string, any>>({});
  const [importing, setImporting] = useState(false);

  const {
    data,
    total,
    loading,
    modules,
    moduleTree,
    environments,
    selectedEnvironment,
    setSelectedEnvironment,
    fetchData,
    fetchModules,
  } = useTestCaseData(projectId, selectedModuleId, page, pageSize, submittedQuery, 'api');

  useEffect(() => {
    axios.get('/api/projects/').then((res) => {
      const list = Array.isArray(res.data) ? res.data : (res.data?.items || []);
      setProjects(list);
      const wanted = Number(new URLSearchParams(window.location.search).get('project_id') || 0);
      if (wanted && list.some((p: Project) => p.id === wanted)) setProjectId(wanted);
      else if (list.length) setProjectId(list[0].id);
    }).catch(() => setProjects([]));
  }, []);

  const moduleName = useMemo(() => {
    const map = new Map<number, string>();
    (modules || []).forEach((m: Module) => map.set(m.id, m.name));
    return map;
  }, [modules]);

  const treeData = useMemo(() => toTreeData(moduleTree as Module[]), [moduleTree]);

  useEffect(() => {
    const ids = (data || []).map((item: any) => item.id);
    if (!projectId || ids.length === 0) {
      setLastRuns({});
      return;
    }
    axios.get('/api/testcases/last-runs', {
      params: { project_id: projectId, case_ids: ids.join(',') },
    }).then((res) => setLastRuns(res.data || {})).catch(() => setLastRuns({}));
  }, [data, projectId]);

  const openNew = () => {
    if (!projectId) {
      Message.warning('请先选择项目');
      return;
    }
    history.push(`/api_test/cases/new/edit?project_id=${projectId}`);
  };

  const openFromDefinition = async () => {
    if (!projectId) {
      Message.warning('请先选择项目');
      return;
    }
    setSelectedDefs([]);
    setDefModalVisible(true);
    setDefLoading(true);
    try {
      const res = await axios.get('/api/api-test/definitions', {
        params: { project_id: projectId, page_size: 500 },
      });
      setDefinitions(res.data?.items || []);
    } catch {
      setDefinitions([]);
    } finally {
      setDefLoading(false);
    }
  };

  const createFromDefinitions = () => {
    if (!selectedDefs.length) {
      Message.warning('请选择至少一个接口定义');
      return;
    }
    setDefModalVisible(false);
    history.push(`/api_test/cases/new/edit?project_id=${projectId}&definitions=${selectedDefs.join(',')}`);
  };

  const runCases = async (caseIds: number[]) => {
    if (!caseIds.length) return;
    if (!selectedEnvironment) {
      Message.warning('请先在顶部选择执行环境');
      return;
    }
    try {
      const res = await axios.post('/api/testcases/batch-run', {
        case_ids: caseIds,
        environment_id: selectedEnvironment,
      });
      const batchId = res.data?.batch_id || res.data?.id;
      Message.success(`已提交 ${caseIds.length} 条用例${batchId ? `（批次 #${batchId}）` : ''}`);
    } catch (err: any) {
      Message.error(err?.response?.data?.detail || '执行失败');
    }
  };

  const deleteCases = async (caseIds: number[]) => {
    if (!caseIds.length) return;
    try {
      for (const id of caseIds) {
        await axios.delete(`/api/testcases/${id}`);
      }
      Message.success(`已删除 ${caseIds.length} 条用例`);
      setSelectedRowKeys([]);
      fetchData();
      fetchModules();
    } catch (err: any) {
      Message.error(err?.response?.data?.detail || '删除失败');
    }
  };

  const columns = [
    {
      title: '用例名称',
      dataIndex: 'name',
      render: (value: string, record: any) => (
        <a onClick={() => history.push(`/api_test/cases/${record.id}/edit`)}>{value}</a>
      ),
    },
    {
      title: '模块',
      dataIndex: 'module_id',
      width: 140,
      render: (value: number | null) => (value ? (moduleName.get(value) || `#${value}`) : '未分组'),
    },
    {
      title: '步骤',
      width: 80,
      render: (_: unknown, record: any) => (record?.api_spec?.steps || []).length || 0,
    },
    {
      title: '请求摘要',
      render: (_: unknown, record: any) => (
        <Text style={{ fontSize: 12 }}>{stepSummary(record)}</Text>
      ),
    },
    {
      title: '优先级',
      dataIndex: 'priority',
      width: 90,
      render: (value: string) => {
        const color = value === 'high' || value === '高' ? 'red' : value === 'low' || value === '低' ? 'green' : 'orange';
        return <Tag color={color}>{value || 'medium'}</Tag>;
      },
    },
    {
      title: '最近执行',
      width: 150,
      render: (_: unknown, record: any) => {
        const run = lastRuns[String(record.id)];
        if (!run) return <Text type="secondary" style={{ fontSize: 12 }}>—</Text>;
        const color = run.status === 'passed' ? 'green' : run.status === 'failed' ? 'red' : 'gray';
        const when = run.start_time ? new Date(run.start_time).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai' }) : '';
        return (
          <div>
            <Tag color={color}>{run.status}</Tag>
            <div style={{ fontSize: 11, color: 'var(--color-text-3)' }}>{when}</div>
          </div>
        );
      },
    },
    {
      title: '操作',
      width: 220,
      render: (_: unknown, record: any) => (
        <Space size="mini">
          <Button
            size="mini"
            icon={<IconEdit />}
            onClick={() => history.push(`/api_test/cases/${record.id}/edit`)}
          >
            编辑
          </Button>
          <Button size="mini" icon={<IconPlayArrow />} onClick={() => runCases([record.id])}>
            执行
          </Button>
          <Button
            size="mini"
            status="danger"
            icon={<IconDelete />}
            onClick={() => Modal.confirm({
              title: '删除用例',
              content: `确认删除「${record.name}」？`,
              onOk: () => deleteCases([record.id]),
            })}
          >
            删除
          </Button>
        </Space>
      ),
    },
  ];

  const handleImport = async (event: React.ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    event.target.value = '';
    if (!file || !projectId) return;
    setImporting(true);
    try {
      const form = new FormData();
      form.append('file', file);
      const res = await axios.post(`/api/testcases/import?project_id=${projectId}&case_kind=api`, form);
      Message.success(`已导入 ${res.data?.created || 0} 条用例`);
      fetchData();
      fetchModules();
    } catch (err: any) {
      Message.error('导入失败：' + (err?.response?.data?.detail || '未知错误'));
    } finally {
      setImporting(false);
    }
  };

  return (
    <div style={{ padding: 16 }}>
      <Card>
        <Space wrap style={{ marginBottom: 12 }}>
          <Select
            style={{ width: 220 }}
            value={projectId ?? undefined}
            placeholder="选择项目"
            showSearch
            onChange={(value) => {
              setProjectId(Number(value));
              setSelectedModuleId(null);
              setPage(1);
              setSelectedRowKeys([]);
            }}
            options={projects.map((p) => ({ label: p.name, value: p.id }))}
          />
          <Select
            style={{ width: 180 }}
            value={selectedEnvironment ?? undefined}
            placeholder="执行环境"
            onChange={(value) => setSelectedEnvironment(Number(value))}
            options={(environments || []).map((e: any) => ({ label: e.name, value: e.id }))}
          />
          <Input.Search
            style={{ width: 260 }}
            value={searchInput}
            placeholder="搜索用例名称"
            onChange={setSearchInput}
            onSearch={(value) => {
              setPage(1);
              setSubmittedQuery(value);
            }}
            onClear={() => {
              setSearchInput('');
              setSubmittedQuery('');
            }}
          />
          <Button type="primary" icon={<IconPlus />} onClick={openNew}>新建空白用例</Button>
          <Button icon={<IconImport />} onClick={openFromDefinition}>从接口定义新建</Button>
          <Button onClick={() => window.open(`/api/testcases/export?project_id=${projectId}&case_kind=api`, '_blank')}>
            导出 xlsx
          </Button>
          <input
            id="api-cases-import"
            type="file"
            accept=".xlsx,.xls"
            style={{ display: 'none' }}
            onChange={handleImport}
          />
          <Button loading={importing} onClick={() => document.getElementById('api-cases-import')?.click()}>
            导入 xlsx
          </Button>
        </Space>

        {selectedRowKeys.length > 0 && (
          <Space style={{ marginBottom: 12 }}>
            <Text>已选 {selectedRowKeys.length} 项</Text>
            <Button type="primary" icon={<IconPlayArrow />} onClick={() => runCases(selectedRowKeys)}>
              批量执行
            </Button>
            <Button
              status="danger"
              onClick={() => Modal.confirm({
                title: '批量删除',
                content: `确认删除已选的 ${selectedRowKeys.length} 条用例？`,
                onOk: () => deleteCases(selectedRowKeys),
              })}
            >
              批量删除
            </Button>
          </Space>
        )}

        <div style={{ display: 'flex', gap: 16 }}>
          <Card style={{ width: 240, flex: '0 0 240px', alignSelf: 'flex-start' }} bodyStyle={{ padding: 8 }}>
            <Button
              size="small"
              type={selectedModuleId === null ? 'primary' : 'secondary'}
              long
              style={{ marginBottom: 8 }}
              onClick={() => { setSelectedModuleId(null); setPage(1); }}
            >
              全部用例
            </Button>
            <Tree
              treeData={treeData}
              selectedKeys={selectedModuleId ? [String(selectedModuleId)] : []}
              onSelect={(keys) => {
                setSelectedModuleId(keys.length ? Number(keys[0]) : null);
                setPage(1);
                setSelectedRowKeys([]);
              }}
            />
          </Card>

          <div style={{ flex: 1, minWidth: 0 }}>
            <Table
              rowKey="id"
              loading={loading}
              columns={columns as any}
              data={data}
              rowSelection={{
                selectedRowKeys,
                onChange: (keys: (string | number)[]) => setSelectedRowKeys(keys as number[]),
              }}
              pagination={{
                total,
                current: page,
                pageSize,
                showTotal: true,
                sizeCanChange: true,
                onChange: (p: number, size: number) => { setPage(p); setPageSize(size); },
              }}
            />
          </div>
        </div>
      </Card>

      <Modal
        title="从接口定义新建用例"
        visible={defModalVisible}
        onCancel={() => setDefModalVisible(false)}
        onOk={createFromDefinitions}
        confirmLoading={defLoading}
        style={{ width: 760 }}
      >
        <Text type="secondary" style={{ fontSize: 12 }}>
          选中的接口会按顺序生成请求步骤，可在编辑器中继续调整。
        </Text>
        <Table
          style={{ marginTop: 12 }}
          rowKey="id"
          loading={defLoading}
          data={definitions}
          pagination={false}
          rowSelection={{
            selectedRowKeys: selectedDefs,
            onChange: (keys: (string | number)[]) => setSelectedDefs(keys as number[]),
          }}
          columns={[
            { title: '方法', dataIndex: 'method', width: 80 },
            { title: '路径', dataIndex: 'path' },
            { title: '名称', dataIndex: 'name' },
          ]}
        />
      </Modal>
    </div>
  );
};

export default ApiCasesPage;
