import React, { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Button, Card, DatePicker, Descriptions, Drawer, Empty, Form, Grid, Input, Message, Modal,
  Popconfirm, Progress, Select, Space, Table, Tabs, Tag, Tooltip,
} from '@arco-design/web-react';
import {
  IconDelete, IconEdit, IconPlayArrow, IconPlus, IconRefresh, IconStop,
} from '@arco-design/web-react/icon';
import axios from 'axios';
import { useHistory } from 'react-router-dom';
import { apiGet } from '@/utils/apiRequest';
import CollabDrawer from './CollabDrawer';

interface Project { id: number; name: string }
interface Member { id: number; username: string; nickname?: string | null; role?: string }
interface Environment { id: number; name: string }

interface PlanStats {
  total: number; not_started: number; in_progress: number; passed: number;
  failed: number; blocked: number; progress: number;
}

interface Plan {
  id: number; project_id: number; name: string; description?: string | null;
  status: string; owner_id?: number | null; start_date?: string | null;
  due_date?: string | null; stats: PlanStats; cycle_count?: number;
  last_cycle?: { id: number; name: string; status: string } | null;
  created_at?: string;
}

interface PlanItem {
  id: number; plan_id: number; case_id: number | null; case_name: string;
  case_kind?: string; assignee_id?: number | null; assignee_name?: string | null;
  priority: string; due_date?: string | null; status: string; note?: string | null;
  case_exists?: boolean; updated_at?: string;
}

interface Cycle {
  id: number; plan_id: number; name: string; status: string; batch_id?: number | null;
  case_count: number; skipped_count: number; environment_id?: number | null;
  passed: number; failed: number; created_at?: string; finished_at?: string | null;
}

const PLAN_STATUS: Record<string, { label: string; color: string }> = {
  draft: { label: '草稿', color: 'gray' },
  active: { label: '进行中', color: 'blue' },
  completed: { label: '已完成', color: 'green' },
  archived: { label: '已归档', color: 'gray' },
};

const ITEM_STATUS: Record<string, { label: string; color: string }> = {
  not_started: { label: '未开始', color: 'gray' },
  in_progress: { label: '进行中', color: 'blue' },
  passed: { label: '通过', color: 'green' },
  failed: { label: '失败', color: 'red' },
  blocked: { label: '阻塞', color: 'orange' },
};

const CYCLE_STATUS: Record<string, { label: string; color: string }> = {
  planned: { label: '待启动', color: 'gray' },
  running: { label: '执行中', color: 'blue' },
  completed: { label: '已完成', color: 'green' },
  cancelled: { label: '已取消', color: 'gray' },
};

const PRIORITY: Record<string, { label: string; color: string }> = {
  low: { label: '低', color: 'gray' },
  medium: { label: '中', color: 'blue' },
  high: { label: '高', color: 'orange' },
  urgent: { label: '紧急', color: 'red' },
};

const Plans: React.FC = () => {
  const history = useHistory();
  const [projects, setProjects] = useState<Project[]>([]);
  const [projectId, setProjectId] = useState<number | null>(null);
  const [plans, setPlans] = useState<Plan[]>([]);
  const [members, setMembers] = useState<Member[]>([]);
  const [environments, setEnvironments] = useState<Environment[]>([]);
  const [currentUserId, setCurrentUserId] = useState<number | null>(null);
  const [loading, setLoading] = useState(false);
  const [listStatus, setListStatus] = useState<string | undefined>(undefined);

  const [plan, setPlan] = useState<Plan | null>(null);
  const [items, setItems] = useState<PlanItem[]>([]);
  const [cycles, setCycles] = useState<Cycle[]>([]);
  const [detailVisible, setDetailVisible] = useState(false);
  const [detailTab, setDetailTab] = useState('items');

  const [createVisible, setCreateVisible] = useState(false);
  const [createForm] = Form.useForm();
  const [addVisible, setAddVisible] = useState(false);
  const [caseOptions, setCaseOptions] = useState<{ label: string; value: number }[]>([]);
  const [selectedCaseIds, setSelectedCaseIds] = useState<number[]>([]);
  const [cycleVisible, setCycleVisible] = useState(false);
  const [cycleForm] = Form.useForm();

  const [collabVisible, setCollabVisible] = useState(false);
  const [collabCase, setCollabCase] = useState<{ id: number; name: string } | null>(null);

  const memberMap = useMemo(
    () => Object.fromEntries(members.map((m) => [m.id, m.nickname || m.username])),
    [members],
  );

  useEffect(() => {
    apiGet<Project[]>('/api/projects/').then((list) => {
      const data = list || [];
      setProjects(data);
      const stored = Number(localStorage.getItem('plans.projectId'));
      const qp = Number(new URLSearchParams(window.location.search).get('projectId'));
      const pick = [qp, stored].find((v) => v && data.some((p) => p.id === v));
      if (pick) setProjectId(pick);
      else if (data.length) setProjectId(data[0].id);
    }).catch(() => Message.error('加载项目失败'));
    axios.get('/api/auth/me').then((res) => setCurrentUserId(res.data?.id ?? null)).catch(() => undefined);
  }, []);

  const loadPlans = useCallback(async () => {
    if (!projectId) return;
    setLoading(true);
    try {
      const params: Record<string, unknown> = { project_id: projectId };
      if (listStatus) params.status = listStatus;
      const res = await apiGet<{ items: Plan[] }>('/api/plans', params);
      setPlans(res.items || []);
    } catch (e: any) {
      Message.error(e?.response?.data?.detail || '加载测试计划失败');
    } finally {
      setLoading(false);
    }
  }, [projectId, listStatus]);

  const loadAux = useCallback(async () => {
    if (!projectId) return;
    try {
      const [m, env] = await Promise.all([
        apiGet<{ items: Member[] }>('/api/plans/members', { project_id: projectId }),
        apiGet<Environment[]>(`/api/projects/${projectId}/environments`),
      ]);
      setMembers(m.items || []);
      setEnvironments(env || []);
    } catch {
      setMembers([]);
      setEnvironments([]);
    }
  }, [projectId]);

  useEffect(() => {
    if (projectId) localStorage.setItem('plans.projectId', String(projectId));
    setPlan(null);
    setDetailVisible(false);
    loadPlans();
    loadAux();
  }, [projectId, loadPlans, loadAux]);

  const loadDetail = useCallback(async (planId: number) => {
    try {
      const [p, it, cy] = await Promise.all([
        apiGet<Plan>(`/api/plans/${planId}`),
        apiGet<{ items: PlanItem[] }>(`/api/plans/${planId}/items`),
        apiGet<{ items: Cycle[] }>(`/api/plans/${planId}/cycles`),
      ]);
      setPlan(p);
      setItems(it.items || []);
      setCycles(cy.items || []);
    } catch (e: any) {
      Message.error(e?.response?.data?.detail || '加载计划详情失败');
    }
  }, []);

  useEffect(() => {
    if (!projectId) return;
    const pid = Number(new URLSearchParams(window.location.search).get('plan_id'));
    if (pid) {
      setDetailVisible(true);
      loadDetail(pid);
    }
  }, [projectId, loadDetail]);

  const openDetail = (row: Plan) => {
    setDetailTab('items');
    setDetailVisible(true);
    loadDetail(row.id);
  };

  const refresh = () => {
    loadPlans();
    if (plan) loadDetail(plan.id);
  };

  const createPlan = async () => {
    try {
      const values = await createForm.validate();
      await axios.post('/api/plans', { project_id: projectId, ...values });
      Message.success('测试计划已创建');
      setCreateVisible(false);
      createForm.resetFields();
      loadPlans();
    } catch (e: any) {
      if (e?.response) Message.error(e?.response?.data?.detail || '创建失败');
    }
  };

  const updateItem = async (item: PlanItem, patch: Record<string, unknown>) => {
    try {
      await axios.patch(`/api/plans/${item.plan_id}/items/${item.id}`, patch);
      if (plan) loadDetail(plan.id);
    } catch (e: any) {
      Message.error(e?.response?.data?.detail || '更新计划项失败');
    }
  };

  const deleteItem = async (item: PlanItem) => {
    try {
      await axios.delete(`/api/plans/${item.plan_id}/items/${item.id}`);
      if (plan) loadDetail(plan.id);
    } catch (e: any) {
      Message.error(e?.response?.data?.detail || '删除失败');
    }
  };

  const updatePlanStatus = async (status: string) => {
    if (!plan) return;
    try {
      await axios.patch(`/api/plans/${plan.id}`, { status });
      loadDetail(plan.id);
      loadPlans();
    } catch (e: any) {
      Message.error(e?.response?.data?.detail || '更新计划状态失败');
    }
  };

  const openAddCases = async () => {
    if (!projectId) return;
    setSelectedCaseIds([]);
    try {
      const res = await apiGet<{ items: { id: number; name: string }[] }>(
        `/api/testcases/project/${projectId}/testcases`,
        { page: 1, size: 200, case_kind: 'functional' },
      );
      setCaseOptions((res.items || []).map((c) => ({ label: `${c.id} · ${c.name}`, value: c.id })));
      setAddVisible(true);
    } catch {
      Message.error('加载用例失败');
    }
  };

  const addCases = async () => {
    if (!plan || selectedCaseIds.length === 0) return;
    try {
      const res = await axios.post(`/api/plans/${plan.id}/items`, { case_ids: selectedCaseIds });
      Message.success(`已添加 ${res.data?.added ?? 0} 条用例`);
      setAddVisible(false);
      loadDetail(plan.id);
      loadPlans();
    } catch (e: any) {
      Message.error(e?.response?.data?.detail || '添加用例失败');
    }
  };

  const createCycle = async () => {
    if (!plan) return;
    try {
      const values = await cycleForm.validate();
      const payload: Record<string, unknown> = {};
      if (values.name) payload.name = values.name;
      if (values.environment_id) payload.environment_id = values.environment_id;
      await axios.post(`/api/plans/${plan.id}/cycles`, payload);
      Message.success('轮次已创建');
      setCycleVisible(false);
      cycleForm.resetFields();
      loadDetail(plan.id);
    } catch (e: any) {
      if (e?.response) Message.error(e?.response?.data?.detail || '创建轮次失败');
    }
  };

  const cycleAction = async (cycle: Cycle, action: string) => {
    try {
      await axios.post(`/api/cycles/${cycle.id}/${action}`);
      Message.success(action === 'start' ? '轮次已启动' : action === 'cancel' ? '轮次已取消' : '已提交重跑');
      if (plan) loadDetail(plan.id);
    } catch (e: any) {
      Message.error(e?.response?.data?.detail || '操作失败');
    }
  };

  const existingCaseIds = new Set(items.map((i) => i.case_id).filter(Boolean) as number[]);

  const planColumns: any[] = [
    {
      title: '计划', dataIndex: 'name', width: 220,
      render: (_: unknown, row: Plan) => (
        <Space>
          <Button type="text" onClick={() => openDetail(row)}>{row.name}</Button>
          <Tag color={PLAN_STATUS[row.status]?.color || 'gray'}>{PLAN_STATUS[row.status]?.label || row.status}</Tag>
        </Space>
      ),
    },
    {
      title: '进度', dataIndex: 'progress', width: 220,
      render: (_: unknown, row: Plan) => (
        <Tooltip content={`通过 ${row.stats?.passed || 0} / ${row.stats?.total || 0}`}>
          <Progress percent={row.stats?.progress || 0} size="small" />
        </Tooltip>
      ),
    },
    { title: '用例数', width: 90, render: (_: unknown, row: Plan) => row.stats?.total ?? 0 },
    {
      title: '失败', width: 80, render: (_: unknown, row: Plan) =>
        row.stats?.failed ? <Tag color="red">{row.stats.failed}</Tag> : '0',
    },
    { title: '轮次', width: 80, render: (_: unknown, row: Plan) => row.cycle_count ?? 0 },
    { title: '截止', dataIndex: 'due_date', width: 120, render: (v: string) => v || '-' },
    {
      title: '操作', width: 140, render: (_: unknown, row: Plan) => (
        <Space>
          <Button size="mini" onClick={() => openDetail(row)}>详情</Button>
          {row.status === 'draft' && (
            <Button size="mini" type="primary" onClick={async () => {
              try {
                await axios.patch(`/api/plans/${row.id}`, { status: 'active' });
                Message.success('计划已激活');
                loadPlans();
              } catch (e: any) {
                Message.error(e?.response?.data?.detail || '激活失败');
              }
            }}>激活</Button>
          )}
        </Space>
      ),
    },
  ];

  const itemColumns: any[] = [
    { title: '用例', dataIndex: 'case_name', width: 240, render: (v: string, row: PlanItem) => (
      <Space>
        <span>{v}</span>
        {row.case_exists === false && <Tag color="gray">已删除</Tag>}
      </Space>
    )},
    {
      title: '状态', width: 130, render: (_: unknown, row: PlanItem) => (
        <Select size="mini" value={row.status} style={{ width: 100 }}
          onChange={(v) => updateItem(row, { status: v })}>
          {Object.entries(ITEM_STATUS).map(([k, m]) => (
            <Select.Option key={k} value={k}>{m.label}</Select.Option>
          ))}
        </Select>
      ),
    },
    {
      title: '负责人', width: 150, render: (_: unknown, row: PlanItem) => (
        <Select size="mini" allowClear placeholder="未指派" style={{ width: 130 }}
          value={row.assignee_id ?? undefined}
          onChange={(v) => updateItem(row, { assignee_id: v ?? null })}>
          {members.map((m) => (
            <Select.Option key={m.id} value={m.id}>{m.nickname || m.username}</Select.Option>
          ))}
        </Select>
      ),
    },
    {
      title: '优先级', width: 110, render: (_: unknown, row: PlanItem) => (
        <Select size="mini" value={row.priority} style={{ width: 90 }}
          onChange={(v) => updateItem(row, { priority: v })}>
          {Object.entries(PRIORITY).map(([k, m]) => (
            <Select.Option key={k} value={k}>{m.label}</Select.Option>
          ))}
        </Select>
      ),
    },
    {
      title: '截止日', width: 150, render: (_: unknown, row: PlanItem) => (
        <DatePicker size="mini" style={{ width: 130 }} value={row.due_date || undefined}
          onChange={(v) => updateItem(row, { due_date: v || null })} />
      ),
    },
    {
      title: '操作', width: 150, render: (_: unknown, row: PlanItem) => (
        <Space>
          <Button size="mini" onClick={() => {
            if (row.case_id) {
              setCollabCase({ id: row.case_id, name: row.case_name });
              setCollabVisible(true);
            }
          }}>评论</Button>
          <Popconfirm title="从计划中移除这条用例？" onOk={() => deleteItem(row)}>
            <Button size="mini" type="text" status="danger" icon={<IconDelete />} />
          </Popconfirm>
        </Space>
      ),
    },
  ];

  const cycleColumns: any[] = [
    { title: '轮次', dataIndex: 'name', width: 200 },
    {
      title: '状态', width: 100, render: (_: unknown, row: Cycle) => (
        <Tag color={CYCLE_STATUS[row.status]?.color || 'gray'}>{CYCLE_STATUS[row.status]?.label || row.status}</Tag>
      ),
    },
    { title: '用例', width: 80, render: (_: unknown, row: Cycle) => row.case_count },
    { title: '通过', width: 80, render: (_: unknown, row: Cycle) => row.passed },
    { title: '失败', width: 80, render: (_: unknown, row: Cycle) => (
      row.failed ? <Tag color="red">{row.failed}</Tag> : 0
    )},
    {
      title: '操作', render: (_: unknown, row: Cycle) => (
        <Space wrap>
          {row.status === 'planned' && (
            <Button size="mini" type="primary" icon={<IconPlayArrow />} onClick={() => cycleAction(row, 'start')}>开始</Button>
          )}
          {row.status === 'running' && (
            <Popconfirm title="取消该轮次及其批次？" onOk={() => cycleAction(row, 'cancel')}>
              <Button size="mini" status="danger" icon={<IconStop />}>取消</Button>
            </Popconfirm>
          )}
          {(row.status === 'completed' || row.status === 'cancelled') && row.failed > 0 && (
            <Button size="mini" onClick={() => cycleAction(row, 'retry-failed')}>重跑失败</Button>
          )}
          {row.batch_id && (
            <Button size="mini" type="text" onClick={() => history.push(`/reports?batch_id=${row.batch_id}`)}>
              批次 #{row.batch_id}
            </Button>
          )}
        </Space>
      ),
    },
  ];

  return (
    <Card>
      <Space style={{ marginBottom: 16 }} wrap>
        <Select
          style={{ width: 240 }} placeholder="选择项目" value={projectId ?? undefined}
          onChange={(v) => setProjectId(v as number)}
          options={projects.map((p) => ({ label: p.name, value: p.id }))}
        />
        <Select
          style={{ width: 150 }} placeholder="全部状态" allowClear value={listStatus}
          onChange={(v) => setListStatus(v as string | undefined)}
          options={Object.entries(PLAN_STATUS).map(([k, m]) => ({ label: m.label, value: k }))}
        />
        <Button icon={<IconRefresh />} onClick={refresh}>刷新</Button>
        <Button type="primary" icon={<IconPlus />} disabled={!projectId} onClick={() => setCreateVisible(true)}>
          新建计划
        </Button>
      </Space>

      <Table
        rowKey="id" loading={loading} columns={planColumns} data={plans}
        pagination={false} noDataElement={<Empty description="暂无测试计划" />}
      />

      <Drawer
        width={1040} visible={detailVisible} footer={null}
        title={plan ? `测试计划 · ${plan.name}` : '测试计划'}
        onCancel={() => setDetailVisible(false)}
      >
        {plan && (
          <>
            <Space style={{ marginBottom: 12 }} wrap>
              {plan.status === 'draft' && <Button type="primary" onClick={() => updatePlanStatus('active')}>激活计划</Button>}
              {plan.status === 'active' && (
                <Popconfirm title="标记为已完成？" onOk={() => updatePlanStatus('completed')}>
                  <Button>完成</Button>
                </Popconfirm>
              )}
              {plan.status !== 'archived' && (
                <Popconfirm title="归档后计划只读，确定？" onOk={() => updatePlanStatus('archived')}>
                  <Button status="warning">归档</Button>
                </Popconfirm>
              )}
              <Button icon={<IconPlus />} onClick={openAddCases}>添加用例</Button>
              <Button type="primary" icon={<IconPlayArrow />} onClick={() => setCycleVisible(true)}>新建轮次</Button>
            </Space>
            <Descriptions
              column={4} size="small" style={{ marginBottom: 12 }}
              data={[
                { label: '状态', value: <Tag color={PLAN_STATUS[plan.status]?.color}>{PLAN_STATUS[plan.status]?.label || plan.status}</Tag> },
                { label: '进度', value: `${plan.stats?.passed || 0}/${plan.stats?.total || 0}（${plan.stats?.progress || 0}%）` },
                { label: '开始', value: plan.start_date || '-' },
                { label: '截止', value: plan.due_date || '-' },
              ]}
            />
            <Tabs activeTab={detailTab} onChange={setDetailTab}>
              <Tabs.TabPane key="items" title={`用例（${items.length}）`}>
                <Table rowKey="id" size="small" columns={itemColumns} data={items} pagination={false}
                  noDataElement={<Empty description="尚无用例，点击右上角添加" />} />
              </Tabs.TabPane>
              <Tabs.TabPane key="cycles" title={`轮次（${cycles.length}）`}>
                <Table rowKey="id" size="small" columns={cycleColumns} data={cycles} pagination={false}
                  noDataElement={<Empty description="尚无轮次" />} />
              </Tabs.TabPane>
            </Tabs>
          </>
        )}
      </Drawer>

      <Modal title="新建测试计划" visible={createVisible} onOk={createPlan} onCancel={() => setCreateVisible(false)}>
        <Form form={createForm} layout="vertical">
          <Form.Item label="名称" field="name" rules={[{ required: true, message: '请输入计划名称' }]}>
            <Input placeholder="例如：登录回归" maxLength={200} />
          </Form.Item>
          <Form.Item label="描述" field="description">
            <Input.TextArea autoSize={{ minRows: 2, maxRows: 4 }} />
          </Form.Item>
          <Grid.Row gutter={16}>
            <Grid.Col span={12}>
              <Form.Item label="开始日期" field="start_date">
                <DatePicker style={{ width: '100%' }} />
              </Form.Item>
            </Grid.Col>
            <Grid.Col span={12}>
              <Form.Item label="截止日期" field="due_date">
                <DatePicker style={{ width: '100%' }} />
              </Form.Item>
            </Grid.Col>
          </Grid.Row>
        </Form>
      </Modal>

      <Modal title="添加用例" visible={addVisible} onOk={addCases} onCancel={() => setAddVisible(false)} okText="添加">
        <Select
          mode="multiple" placeholder="选择用例（可按名称搜索）" showSearch
          style={{ width: '100%' }} value={selectedCaseIds}
          onChange={(v) => setSelectedCaseIds(v as number[])}
          options={caseOptions.map((o) => ({ ...o, disabled: existingCaseIds.has(o.value) }))}
          filterOption={(input: string, option: any) => String(option?.label || '').toLowerCase().includes(input.toLowerCase())}
        />
        <div style={{ color: 'var(--color-text-3)', marginTop: 8 }}>
          已在计划中的用例不可重复添加。
        </div>
      </Modal>

      <Modal title="新建轮次" visible={cycleVisible} onOk={createCycle} onCancel={() => setCycleVisible(false)}>
        <Form form={cycleForm} layout="vertical">
          <Form.Item label="轮次名称" field="name">
            <Input placeholder="留空自动命名（第 N 轮）" maxLength={200} />
          </Form.Item>
          <Form.Item label="环境" field="environment_id">
            <Select allowClear placeholder="可选（用于接口/UI 执行环境）"
              options={environments.map((e) => ({ label: e.name, value: e.id }))} />
          </Form.Item>
        </Form>
        <div style={{ color: 'var(--color-text-3)' }}>
          创建后按当前计划项生成快照；启动时执行整轮并回填计划项状态。
        </div>
      </Modal>

      <CollabDrawer
        visible={collabVisible}
        caseId={collabCase?.id ?? null}
        caseName={collabCase?.name}
        projectId={projectId}
        members={members}
        currentUserId={currentUserId}
        onClose={() => setCollabVisible(false)}
      />
    </Card>
  );
};

export default Plans;
