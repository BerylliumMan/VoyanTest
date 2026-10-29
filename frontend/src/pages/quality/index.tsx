import React, { useCallback, useEffect, useState } from 'react';
import {
  Button, Card, Empty, Input, Message, Modal, Select, Space, Table, Tabs, Tag,
} from '@arco-design/web-react';
import axios from 'axios';

const api = axios.create({ baseURL: '', timeout: 120000 });

interface Project { id: number; name: string }
interface Candidate {
  case_id: number; case_kind: string; name: string;
  reasons: { type: string; ref: string; detail: string }[];
}
interface Plan {
  id: number; name: string; environment_id?: number; agent_id?: number;
  items: any[]; last_batch_id?: number; created_at?: string;
}
interface Flaky {
  case_id: number; status: string; sample_runs: number; passed: number; failed: number;
  pass_rate?: number; consecutive_passes: number; reason?: string;
}
interface Req { id: number; name: string; module_id?: number; coverage?: string; case_count?: number; passed?: number; failed?: number }

const REASON_COLORS: Record<string, string> = {
  definition_change: 'arcoblue', recent_failure: 'red', stale: 'orange',
  flaky: 'purple', manual: 'gray', module_risk: 'gold',
};
const REASON_LABELS: Record<string, string> = {
  definition_change: '定义变更', recent_failure: '近期失败', stale: '长期未执行',
  flaky: 'Flaky', manual: '手工', module_risk: '模块风险',
};

function ImpactPanel({ projectId }: { projectId: number }) {
  const [candidates, setCandidates] = useState<Candidate[]>([]);
  const [notes, setNotes] = useState<string[]>([]);
  const [baseline, setBaseline] = useState('');
  const [loading, setLoading] = useState(false);
  const [plans, setPlans] = useState<Plan[]>([]);
  const [saveVisible, setSaveVisible] = useState(false);
  const [planName, setPlanName] = useState('');
  const [selected, setSelected] = useState<number[]>([]);

  const loadPlans = useCallback(async () => {
    if (!projectId) return;
    const res = await api.get('/api/quality/regression-plans', { params: { project_id: projectId } });
    setPlans(res.data.items || []);
  }, [projectId]);

  useEffect(() => { loadPlans().catch(() => undefined); }, [loadPlans]);

  const analyze = async () => {
    setLoading(true);
    try {
      const res = await api.post('/api/quality/impact-analysis', { project_id: projectId });
      setCandidates(res.data.candidates || []);
      setSelected((res.data.candidates || []).map((c: Candidate) => c.case_id));
      setNotes(res.data.notes || []);
      setBaseline(`${res.data.baseline_source || ''} ${res.data.baseline_at || ''}`);
      if (!(res.data.candidates || []).length) Message.info('没有匹配信号');
    } catch (e: any) {
      Message.error(e?.response?.data?.detail || '影响分析失败');
    } finally { setLoading(false); }
  };

  const save = async () => {
    if (!planName.trim()) { Message.warning('请输入计划名称'); return; }
    try {
      await api.post('/api/quality/regression-plans', {
        project_id: projectId, name: planName,
        items: candidates.filter((c) => selected.includes(c.case_id)).map((c) => ({
          case_id: c.case_id, case_kind: c.case_kind, reasons: c.reasons,
        })),
      });
      Message.success('回归计划已保存');
      setSaveVisible(false); setPlanName('');
      await loadPlans();
    } catch (e: any) { Message.error(e?.response?.data?.detail || '保存失败'); }
  };

  const runPlan = async (plan: Plan) => {
    try {
      const res = await api.post(`/api/quality/regression-plans/${plan.id}/run`, {
        include_init: true, run_mode: plan.agent_id ? 'client' : 'server',
      });
      Message.success(`已提交批次 #${res.data.batch_id}`);
      await loadPlans();
    } catch (e: any) { Message.error(e?.response?.data?.detail || '执行失败'); }
  };

  return (
    <Space direction="vertical" style={{ width: '100%' }} size="medium">
      <Card>
        <Space>
          <Button type="primary" loading={loading} onClick={analyze}>分析影响</Button>
          <Button disabled={!candidates.length} onClick={() => setSaveVisible(true)}>保存为回归计划</Button>
          {baseline && <Tag>基线：{baseline}</Tag>}
          {notes.map((n) => <Tag key={n} color="orange">{n}</Tag>)}
        </Space>
      </Card>
      <Card title={`候选用例（${candidates.length}）`}>
        {candidates.length ? (
          <Table
            rowKey="case_id"
            data={candidates}
            pagination={{ pageSize: 20 }}
            rowSelection={{ selectedRowKeys: selected, onChange: (keys) => setSelected(keys as number[]) }}
            columns={[
              { title: 'ID', dataIndex: 'case_id', width: 80 },
              { title: '名称', dataIndex: 'name' },
              { title: '类型', dataIndex: 'case_kind', width: 90 },
              {
                title: '理由', dataIndex: 'reasons',
                render: (reasons: Candidate['reasons']) => (
                  <Space wrap>
                    {reasons.map((r, i) => (
                      <Tag key={i} color={REASON_COLORS[r.type] || 'gray'}>
                        {REASON_LABELS[r.type] || r.type}{r.detail ? `：${r.detail}` : ''}
                      </Tag>
                    ))}
                  </Space>
                ),
              },
            ]}
          />
        ) : <Empty description="点击「分析影响」生成候选" />}
      </Card>
      <Card title="已保存的回归计划">
        <Table
          rowKey="id" data={plans} pagination={false}
          columns={[
            { title: '名称', dataIndex: 'name' },
            { title: '用例数', render: (_: any, r: Plan) => (r.items || []).length, width: 90 },
            { title: '环境', dataIndex: 'environment_id', width: 90 },
            { title: '最近批次', dataIndex: 'last_batch_id', width: 110 },
            {
              title: '操作', width: 160,
              render: (_: any, r: Plan) => (
                <Space>
                  <Button size="mini" onClick={() => runPlan(r)}>执行</Button>
                  <Button size="mini" status="danger" onClick={async () => {
                    await api.delete(`/api/quality/regression-plans/${r.id}`);
                    await loadPlans();
                  }}>删除</Button>
                </Space>
              ),
            },
          ]}
        />
      </Card>
      <Modal title="保存回归计划" visible={saveVisible} onOk={save}
        onCancel={() => setSaveVisible(false)}>
        <Input placeholder="计划名称" value={planName} onChange={setPlanName} />
        <div style={{ marginTop: 8, color: 'var(--color-text-2)' }}>
          已选 {selected.length} 条用例；执行时自动带项目初始化用例。
        </div>
      </Modal>
    </Space>
  );
}

function FlakyPanel({ projectId }: { projectId: number }) {
  const [rows, setRows] = useState<Flaky[]>([]);
  const [envs, setEnvs] = useState<{ id: number; name: string }[]>([]);
  const [envId, setEnvId] = useState<number>();
  const [loading, setLoading] = useState(false);

  const load = useCallback(async () => {
    if (!projectId) return;
    setLoading(true);
    try {
      const res = await api.get('/api/quality/flaky', { params: { project_id: projectId } });
      setRows(res.data.items || []);
    } finally { setLoading(false); }
  }, [projectId]);

  useEffect(() => {
    load().catch(() => undefined);
    api.get(`/api/projects/${projectId}/environments`).then((r) => {
      const list = r.data || [];
      setEnvs(list);
      const def = list.find((e: any) => e.is_default) || list[0];
      setEnvId(def?.id);
    }).catch(() => undefined);
  }, [load, projectId]);

  const confirm = async (caseId: number) => {
    try {
      const res = await api.post(`/api/quality/flaky/${caseId}/confirm`, { environment_id: envId });
      Message.success(`复跑结果：${res.data.status}`);
      await load();
    } catch (e: any) { Message.error(e?.response?.data?.detail || '确认失败'); }
  };
  const quarantine = async (caseId: number) => {
    await api.post(`/api/quality/flaky/${caseId}/quarantine`, { reason: '人工隔离' });
    await load();
  };
  const release = async (caseId: number) => {
    await api.post(`/api/quality/flaky/${caseId}/release`, {});
    await load();
  };

  return (
    <Space direction="vertical" style={{ width: '100%' }}>
      <Card>
        <Space>
          <span>确认复跑环境：</span>
          <Select style={{ width: 220 }} value={envId} onChange={setEnvId}
            options={envs.map((e) => ({ label: e.name, value: e.id }))} />
          <Button onClick={load} loading={loading}>刷新</Button>
          <span style={{ color: 'var(--color-text-2)' }}>隔离只影响自动执行；手工执行仍可运行。</span>
        </Space>
      </Card>
      <Card title={`Flaky 候选 / 隔离（${rows.length}）`}>
        <Table rowKey="case_id" data={rows} loading={loading} pagination={{ pageSize: 20 }}
          columns={[
            { title: '用例', dataIndex: 'case_id', width: 90 },
            { title: '状态', dataIndex: 'status', width: 120, render: (s: string) => <Tag color={s === 'quarantined' ? 'red' : s === 'recovered' ? 'green' : 'orange'}>{s}</Tag> },
            { title: '样本', dataIndex: 'sample_runs', width: 80 },
            { title: '通过/失败', render: (_: any, r: Flaky) => `${r.passed} / ${r.failed}`, width: 110 },
            { title: '通过率', render: (_: any, r: Flaky) => r.pass_rate == null ? '-' : `${r.pass_rate}%`, width: 90 },
            { title: '连续通过', dataIndex: 'consecutive_passes', width: 100 },
            { title: '原因', dataIndex: 'reason' },
            {
              title: '操作', width: 230,
              render: (_: any, r: Flaky) => (
                <Space>
                  {r.status !== 'quarantined' && <Button size="mini" onClick={() => confirm(r.case_id)}>确认不稳定</Button>}
                  {r.status !== 'quarantined' && <Button size="mini" status="danger" onClick={() => quarantine(r.case_id)}>隔离</Button>}
                  {r.status === 'quarantined' && <Button size="mini" onClick={() => release(r.case_id)}>解除</Button>}
                </Space>
              ),
            },
          ]} />
      </Card>
    </Space>
  );
}

function RequirementPanel({ projectId }: { projectId: number }) {
  const [reqs, setReqs] = useState<Req[]>([]);
  const [matrix, setMatrix] = useState<any>(null);
  const [sessionId, setSessionId] = useState('');
  const [kind, setKind] = useState('all');
  const [linkVisible, setLinkVisible] = useState(false);
  const [linkReq, setLinkReq] = useState<number>();
  const [caseIds, setCaseIds] = useState('');
  const [name, setName] = useState('');

  const load = useCallback(async () => {
    if (!projectId) return;
    const [r1, r2] = await Promise.all([
      api.get('/api/quality/requirements', { params: { project_id: projectId } }),
      api.get('/api/quality/requirements/matrix', { params: { project_id: projectId, case_kind: kind } }),
    ]);
    setReqs(r1.data.items || []);
    setMatrix(r2.data);
  }, [kind, projectId]);

  useEffect(() => { load().catch(() => undefined); }, [load]);

  const importFps = async () => {
    if (!sessionId.trim()) { Message.warning('请输入生成会话 ID'); return; }
    try {
      const res = await api.post('/api/quality/requirements/import', {
        project_id: projectId, session_id: sessionId.trim(),
      });
      Message.success(`导入 ${res.data.created} 条，跳过 ${res.data.skipped} 条`);
      await load();
    } catch (e: any) { Message.error(e?.response?.data?.detail || '导入失败'); }
  };

  const createReq = async () => {
    if (!name.trim()) return;
    try {
      await api.post('/api/quality/requirements', { project_id: projectId, name: name.trim() });
      setName(''); await load();
    } catch (e: any) { Message.error(e?.response?.data?.detail || '创建失败'); }
  };

  const link = async () => {
    if (!linkReq) return;
    const ids = caseIds.split(',').map((x) => Number(x.trim())).filter(Boolean);
    await api.post(`/api/quality/requirements/${linkReq}/links`, { case_ids: ids });
    setLinkVisible(false); setCaseIds('');
    await load();
  };

  return (
    <Space direction="vertical" style={{ width: '100%' }}>
      <Card>
        <Space wrap>
          <Input style={{ width: 300 }} placeholder="生成会话 ID（从生成历史详情复制）"
            value={sessionId} onChange={setSessionId} />
          <Button onClick={importFps}>导入功能点</Button>
          <Input style={{ width: 220 }} placeholder="新建需求名称" value={name} onChange={setName} />
          <Button onClick={createReq}>新建需求</Button>
          <Select style={{ width: 140 }} value={kind} onChange={setKind}
            options={[{ label: '全部类型', value: 'all' }, { label: 'UI', value: 'ui' }, { label: '功能', value: 'functional' }, { label: '接口', value: 'api' }]} />
        </Space>
      </Card>
      <Card title={`需求覆盖矩阵（${reqs.length}）`}>
        <Table rowKey="id" data={matrix?.requirements || reqs} pagination={{ pageSize: 20 }}
          columns={[
            { title: '需求', dataIndex: 'name' },
            { title: '关联用例', render: (_: any, r: Req) => r.case_count ?? 0, width: 100 },
            { title: '通过/失败', render: (_: any, r: Req) => `${r.passed ?? 0} / ${r.failed ?? 0}`, width: 120 },
            { title: '覆盖', dataIndex: 'coverage', width: 100, render: (c: string) => <Tag color={c === 'uncovered' ? 'red' : c === 'partial' ? 'orange' : 'green'}>{c || '-'}</Tag> },
            {
              title: '操作', width: 120,
              render: (_: any, r: Req) => <Button size="mini" onClick={() => { setLinkReq(r.id); setLinkVisible(true); }}>关联用例</Button>,
            },
          ]} />
      </Card>
      <Card title={`无来源用例（${(matrix?.unlinked_cases || []).length}）`}>
        <Table rowKey="case_id" data={matrix?.unlinked_cases || []} pagination={{ pageSize: 10 }}
          columns={[{ title: 'ID', dataIndex: 'case_id', width: 80 }, { title: '名称', dataIndex: 'name' }, { title: '类型', dataIndex: 'case_kind', width: 90 }]} />
      </Card>
      <Modal title="关联用例" visible={linkVisible} onOk={link} onCancel={() => setLinkVisible(false)}>
        <Input placeholder="用例 ID，逗号分隔，例如 12,13" value={caseIds} onChange={setCaseIds} />
      </Modal>
    </Space>
  );
}

export default function QualityPage() {
  const [projects, setProjects] = useState<Project[]>([]);
  const [projectId, setProjectId] = useState<number>();

  useEffect(() => {
    api.get('/api/projects/').then((r) => {
      const list = Array.isArray(r.data) ? r.data : (r.data.items || []);
      setProjects(list);
      if (list.length) setProjectId(list[0].id);
    }).catch(() => undefined);
  }, []);

  return (
    <div style={{ padding: 16 }}>
      <Card style={{ marginBottom: 12 }}>
        <Space>
          <span>项目：</span>
          <Select style={{ width: 260 }} value={projectId} onChange={setProjectId}
            options={projects.map((p) => ({ label: p.name, value: p.id }))} />
        </Space>
      </Card>
      {projectId ? (
        <Tabs type="rounded">
          <Tabs.TabPane key="impact" title="回归计划">
            <ImpactPanel projectId={projectId} />
          </Tabs.TabPane>
          <Tabs.TabPane key="flaky" title="Flaky 治理">
            <FlakyPanel projectId={projectId} />
          </Tabs.TabPane>
          <Tabs.TabPane key="requirements" title="需求追溯">
            <RequirementPanel projectId={projectId} />
          </Tabs.TabPane>
        </Tabs>
      ) : <Empty description="请选择项目" />}
    </div>
  );
}
