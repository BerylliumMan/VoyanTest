import React, { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Button, Card, Checkbox, DatePicker, Drawer, Empty, Input, InputNumber, Message,
  Modal, Popconfirm, Select, Space, Spin, Table, Tabs, Tag,
} from '@arco-design/web-react';
import axios from 'axios';

const api = axios.create({ baseURL: '', timeout: 120000 });

interface Project { id: number; name: string }
interface Env { id: number; name: string }
interface AgentItem { id: number; name: string }
interface CaseItem { id: number; name: string; case_kind?: string }
interface LinkedCase { case_id: number; name: string; case_kind?: string }
interface Reason { type: string; ref: string; detail: string }
interface Candidate { case_id: number; case_kind: string; name: string; reasons: Reason[]; note?: string }
interface PlanItem { case_id: number; case_kind?: string; reasons?: Reason[]; note?: string }
interface Plan {
  id: number; name: string; description?: string;
  environment_id?: number; agent_id?: number;
  environment_name?: string; agent_name?: string;
  items: PlanItem[]; last_batch_id?: number;
  last_batch_status?: string; last_batch_time?: string;
}
interface Flaky {
  case_id: number; name: string; status: string; sample_runs: number;
  passed: number; failed: number; pass_rate?: number;
  consecutive_passes: number; last_failed_at?: string; reason?: string;
}
interface Req {
  id: number; name: string; description?: string; source?: string; status?: string;
  case_count?: number; passed?: number; failed?: number; coverage?: string;
}
interface GenSession { id: string; filename?: string; created_at?: string; test_cases_count?: number; status?: string }
interface Overview {
  total_cases: number; runs_14d: number; passed_14d: number; failed_14d: number;
  pass_rate_14d: number | null; flaky_candidates: number; flaky_quarantined: number;
  requirements: number; requirements_uncovered: number; unlinked_cases: number;
}

const REASON_COLORS: Record<string, string> = {
  definition_change: 'arcoblue', recent_failure: 'red', stale: 'orange',
  flaky: 'purple', manual: 'gray', module_risk: 'gold',
};
const REASON_LABELS: Record<string, string> = {
  definition_change: '定义变更', recent_failure: '近期失败', stale: '长期未执行',
  flaky: 'Flaky', manual: '手工', module_risk: '模块风险',
};
const FLAKY_LABELS: Record<string, string> = {
  candidate: '候选', confirming: '确认中', quarantined: '已隔离', recovered: '已恢复',
};
const COVERAGE_LABELS: Record<string, string> = {
  uncovered: '未覆盖', partial: '部分覆盖', covered: '已覆盖',
};
const BATCH_LABELS: Record<string, string> = {
  running: '执行中', passed: '通过', failed: '失败', cancelled: '已取消', partial: '部分通过',
};

function fmtTime(v?: string | null) {
  if (!v) return '-';
  const d = new Date(v);
  if (Number.isNaN(d.getTime())) return v;
  return d.toLocaleString('zh-CN', { hour12: false });
}

function reasonTags(reasons?: Reason[]) {
  return (
    <Space wrap size={4}>
      {(reasons || []).map((r, i) => (
        <Tag key={`${r.type}-${i}`} color={REASON_COLORS[r.type] || 'gray'}>
          {REASON_LABELS[r.type] || r.type}{r.detail ? `：${r.detail}` : ''}
        </Tag>
      ))}
    </Space>
  );
}

function OverviewCards({ projectId, refreshKey }: { projectId: number; refreshKey: number }) {
  const [data, setData] = useState<Overview | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    api.get('/api/quality/overview', { params: { project_id: projectId } })
      .then((r) => { if (alive) setData(r.data); })
      .catch(() => { if (alive) setData(null); })
      .finally(() => { if (alive) setLoading(false); });
    return () => { alive = false; };
  }, [projectId, refreshKey]);

  const stats = [
    { label: '用例总数', value: data?.total_cases ?? '-', hint: '' },
    {
      label: '近 14 天通过率',
      value: data?.pass_rate_14d == null ? '-' : `${data.pass_rate_14d}%`,
      hint: data ? `${data.passed_14d} 通过 / ${data.failed_14d} 失败 / 共 ${data.runs_14d}` : '',
    },
    { label: 'Flaky 候选', value: data?.flaky_candidates ?? '-', hint: '待确认的不稳定用例' },
    { label: '已隔离', value: data?.flaky_quarantined ?? '-', hint: '自动执行时跳过' },
    { label: '需求覆盖缺口', value: data?.requirements_uncovered ?? '-', hint: `共 ${data?.requirements ?? 0} 条需求` },
    { label: '无来源用例', value: data?.unlinked_cases ?? '-', hint: '尚未关联需求' },
  ];

  return (
    <Card style={{ marginBottom: 12 }} loading={loading && !data}>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(6, minmax(0, 1fr))', gap: 12 }}>
        {stats.map((s) => (
          <div key={s.label} style={{ minWidth: 0 }}>
            <div style={{ color: 'var(--color-text-3)', fontSize: 12, whiteSpace: 'nowrap' }}>{s.label}</div>
            <div style={{ fontSize: 24, fontWeight: 600, lineHeight: '32px' }}>{s.value}</div>
            <div style={{ color: 'var(--color-text-3)', fontSize: 12, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={s.hint}>{s.hint}</div>
          </div>
        ))}
      </div>
    </Card>
  );
}

function ImpactPanel({ projectId, onChanged }: { projectId: number; onChanged: () => void }) {
  const [candidates, setCandidates] = useState<Candidate[]>([]);
  const [notes, setNotes] = useState<string[]>([]);
  const [baseline, setBaseline] = useState<{ source?: string; at?: string } | null>(null);
  const [loading, setLoading] = useState(false);
  const [plans, setPlans] = useState<Plan[]>([]);
  const [plansLoading, setPlansLoading] = useState(false);
  const [selected, setSelected] = useState<number[]>([]);

  // 分析设置
  const [recentDays, setRecentDays] = useState(14);
  const [staleDays, setStaleDays] = useState(30);
  const [includeQuarantined, setIncludeQuarantined] = useState(false);
  const [baselineAt, setBaselineAt] = useState<string>();
  // 候选筛选
  const [keyword, setKeyword] = useState('');
  const [reasonFilter, setReasonFilter] = useState('all');
  const [kindFilter, setKindFilter] = useState('all');

  const [envs, setEnvs] = useState<Env[]>([]);
  const [agents, setAgents] = useState<AgentItem[]>([]);

  // 保存
  const [saveVisible, setSaveVisible] = useState(false);
  const [planName, setPlanName] = useState('');
  const [planDesc, setPlanDesc] = useState('');
  const [planEnv, setPlanEnv] = useState<number>();
  const [planAgent, setPlanAgent] = useState<number>();
  // 详情
  const [detailPlan, setDetailPlan] = useState<Plan | null>(null);
  // 编辑
  const [editPlan, setEditPlan] = useState<Plan | null>(null);
  const [editName, setEditName] = useState('');
  const [editDesc, setEditDesc] = useState('');
  const [editEnv, setEditEnv] = useState<number>();
  const [editAgent, setEditAgent] = useState<number>();
  // 执行
  const [runPlan, setRunPlan] = useState<Plan | null>(null);
  const [runEnv, setRunEnv] = useState<number>();
  const [runAgent, setRunAgent] = useState<number>();
  const [runInit, setRunInit] = useState(true);
  const [running, setRunning] = useState(false);

  const loadPlans = useCallback(async () => {
    if (!projectId) return;
    setPlansLoading(true);
    try {
      const res = await api.get('/api/quality/regression-plans', { params: { project_id: projectId } });
      setPlans(res.data.items || []);
    } finally { setPlansLoading(false); }
  }, [projectId]);

  useEffect(() => { loadPlans().catch(() => undefined); }, [loadPlans]);
  useEffect(() => {
    api.get(`/api/projects/${projectId}/environments`)
      .then((r) => setEnvs(r.data || [])).catch(() => undefined);
    api.get('/api/agents')
      .then((r) => setAgents(Array.isArray(r.data) ? r.data : r.data.items || []))
      .catch(() => undefined);
  }, [projectId]);

  const analyze = async () => {
    setLoading(true);
    try {
      const res = await api.post('/api/quality/impact-analysis', {
        project_id: projectId,
        baseline_at: baselineAt || null,
        recent_days: recentDays,
        stale_days: staleDays,
        include_quarantined: includeQuarantined,
      });
      const list: Candidate[] = res.data.candidates || [];
      setCandidates(list);
      setSelected(list.map((c) => c.case_id));
      setNotes(res.data.notes || []);
      setBaseline({ source: res.data.baseline_source, at: res.data.baseline_at });
      if (!list.length) Message.info('没有匹配信号，建议全量执行或手工选择用例');
    } catch (e: any) {
      Message.error(e?.response?.data?.detail || '影响分析失败');
    } finally { setLoading(false); }
  };

  const filtered = useMemo(() => candidates.filter((c) => {
    if (keyword && !c.name.toLowerCase().includes(keyword.toLowerCase()) && !String(c.case_id).includes(keyword)) return false;
    if (kindFilter !== 'all' && c.case_kind !== kindFilter) return false;
    if (reasonFilter !== 'all' && !c.reasons.some((r) => r.type === reasonFilter)) return false;
    return true;
  }), [candidates, keyword, kindFilter, reasonFilter]);

  const openSave = () => {
    if (!selected.length) { Message.warning('请先勾选候选用例'); return; }
    setPlanName(`回归计划 ${new Date().toLocaleDateString('zh-CN')}`);
    setPlanDesc(''); setPlanEnv(undefined); setPlanAgent(undefined);
    setSaveVisible(true);
  };

  const save = async () => {
    if (!planName.trim()) { Message.warning('请输入计划名称'); return; }
    const picked = candidates.filter((c) => selected.includes(c.case_id));
    try {
      await api.post('/api/quality/regression-plans', {
        project_id: projectId, name: planName.trim(), description: planDesc || undefined,
        environment_id: planEnv || null, agent_id: planAgent || null,
        items: picked.map((c) => ({ case_id: c.case_id, case_kind: c.case_kind, reasons: c.reasons })),
      });
      Message.success('回归计划已保存');
      setSaveVisible(false);
      await loadPlans();
      onChanged();
    } catch (e: any) { Message.error(e?.response?.data?.detail || '保存失败'); }
  };

  const openEdit = (plan: Plan) => {
    setEditPlan(plan); setEditName(plan.name); setEditDesc(plan.description || '');
    setEditEnv(plan.environment_id); setEditAgent(plan.agent_id);
  };

  const saveEdit = async () => {
    if (!editPlan) return;
    if (!editName.trim()) { Message.warning('请输入计划名称'); return; }
    try {
      await api.patch(`/api/quality/regression-plans/${editPlan.id}`, {
        name: editName.trim(), description: editDesc || null,
        environment_id: editEnv || null, agent_id: editAgent || null,
      });
      Message.success('已更新');
      setEditPlan(null);
      await loadPlans();
    } catch (e: any) { Message.error(e?.response?.data?.detail || '更新失败'); }
  };

  const openRun = (plan: Plan) => {
    setRunPlan(plan); setRunEnv(plan.environment_id); setRunAgent(plan.agent_id);
    setRunInit(true);
  };

  const doRun = async () => {
    if (!runPlan) return;
    setRunning(true);
    try {
      const res = await api.post(`/api/quality/regression-plans/${runPlan.id}/run`, {
        include_init: runInit,
        run_mode: runAgent ? 'client' : 'server',
        environment_id: runEnv || null,
        agent_id: runAgent || null,
      });
      Message.success(`已提交批次 #${res.data.batch_id}${(res.data.invalid_case_ids || []).length ? `，${res.data.invalid_case_ids.length} 条用例已失效被跳过` : ''}`);
      setRunPlan(null);
      await loadPlans();
      onChanged();
    } catch (e: any) { Message.error(e?.response?.data?.detail || '执行失败'); }
    finally { setRunning(false); }
  };

  const removePlan = async (plan: Plan) => {
    try {
      await api.delete(`/api/quality/regression-plans/${plan.id}`);
      Message.success('已删除');
      await loadPlans();
    } catch (e: any) { Message.error(e?.response?.data?.detail || '删除失败'); }
  };

  const baselineText = baseline?.at
    ? `${baseline.source === 'last_success_run' ? '最近成功执行' : baseline.source === 'last_plan' ? '最近回归计划' : '手工指定'} · ${fmtTime(baseline.at)}`
    : '';

  return (
    <Space direction="vertical" style={{ width: '100%' }} size="medium">
      <Card>
        <Space wrap size="medium" style={{ marginBottom: 8 }}>
          <Button type="primary" loading={loading} onClick={analyze}>分析影响</Button>
          <Button disabled={!selected.length} onClick={openSave}>保存为回归计划</Button>
          <span style={{ color: 'var(--color-text-2)' }}>近期失败</span>
          <InputNumber min={1} max={365} value={recentDays} onChange={(v) => setRecentDays(Number(v) || 14)} style={{ width: 80 }} />
          <span style={{ color: 'var(--color-text-2)' }}>天 · 未执行</span>
          <InputNumber min={1} max={3650} value={staleDays} onChange={(v) => setStaleDays(Number(v) || 30)} style={{ width: 90 }} />
          <span style={{ color: 'var(--color-text-2)' }}>天</span>
          <Checkbox checked={includeQuarantined} onChange={setIncludeQuarantined}>包含已隔离</Checkbox>
          <span style={{ color: 'var(--color-text-2)' }}>基线</span>
          <DatePicker showTime style={{ width: 200 }} value={baselineAt} onChange={(v) => setBaselineAt(v)} placeholder="默认最近成功执行" />
        </Space>
        <Space wrap>
          {baselineText && <Tag color="arcoblue">基线：{baselineText}</Tag>}
          {notes.map((n) => <Tag key={n} color="orange">{n}</Tag>)}
        </Space>
      </Card>

      <Card title={`候选用例（${filtered.length}${filtered.length !== candidates.length ? ` / 共 ${candidates.length}` : ''}）`}>
        <Space wrap style={{ marginBottom: 12 }}>
          <Input allowClear style={{ width: 220 }} placeholder="搜索用例名称 / ID" value={keyword} onChange={setKeyword} />
          <Select style={{ width: 150 }} value={reasonFilter} onChange={setReasonFilter}
            options={[{ label: '全部理由', value: 'all' }, ...Object.entries(REASON_LABELS).map(([value, label]) => ({ label, value }))]} />
          <Select style={{ width: 130 }} value={kindFilter} onChange={setKindFilter}
            options={[{ label: '全部类型', value: 'all' }, { label: 'UI', value: 'ui' }, { label: '功能', value: 'functional' }, { label: '接口', value: 'api' }]} />
          <span style={{ color: 'var(--color-text-3)' }}>已选 {selected.length} 条</span>
        </Space>
        {candidates.length ? (
          <Table
            rowKey="case_id"
            data={filtered}
            pagination={{ pageSize: 20, showTotal: true }}
            rowSelection={{
              selectedRowKeys: selected,
              onChange: (keys) => setSelected(keys as number[]),
            }}
            columns={[
              { title: 'ID', dataIndex: 'case_id', width: 80 },
              { title: '名称', dataIndex: 'name' },
              { title: '类型', dataIndex: 'case_kind', width: 90 },
              {
                title: '理由', dataIndex: 'reasons',
                render: (reasons: Reason[]) => reasonTags(reasons),
              },
            ]}
          />
        ) : <Empty description="点击「分析影响」生成候选；可调整窗口与基线后重新分析" />}
      </Card>

      <Card title="已保存的回归计划">
        <Table
          rowKey="id" data={plans} loading={plansLoading} pagination={{ pageSize: 10, showTotal: true }}
          columns={[
            { title: '名称', dataIndex: 'name', render: (_: any, r: Plan) => (
              <Space direction="vertical" size={0}>
                <span>{r.name}</span>
                {r.description ? <span style={{ color: 'var(--color-text-3)', fontSize: 12 }}>{r.description}</span> : null}
              </Space>
            ) },
            { title: '用例数', width: 80, render: (_: any, r: Plan) => (r.items || []).length },
            { title: '环境', width: 130, render: (_: any, r: Plan) => r.environment_name || '服务端默认' },
            { title: '客户端', width: 130, render: (_: any, r: Plan) => r.agent_name || '服务端执行' },
            {
              title: '最近批次', width: 170,
              render: (_: any, r: Plan) => r.last_batch_id ? (
                <a href={`/reports?batch_id=${r.last_batch_id}`} target="_blank" rel="noreferrer">
                  #{r.last_batch_id} · {BATCH_LABELS[r.last_batch_status || ''] || r.last_batch_status || '-'}
                </a>
              ) : '-',
            },
            {
              title: '操作', width: 250,
              render: (_: any, r: Plan) => (
                <Space>
                  <Button size="mini" onClick={() => setDetailPlan(r)}>详情</Button>
                  <Button size="mini" onClick={() => openEdit(r)}>编辑</Button>
                  <Button size="mini" type="primary" onClick={() => openRun(r)}>执行</Button>
                  <Popconfirm title={`删除计划「${r.name}」？`} onOk={() => removePlan(r)}>
                    <Button size="mini" status="danger">删除</Button>
                  </Popconfirm>
                </Space>
              ),
            },
          ]}
        />
      </Card>

      <Modal title="保存回归计划" visible={saveVisible} onOk={save} onCancel={() => setSaveVisible(false)} okText="保存">
        <Space direction="vertical" style={{ width: '100%' }}>
          <Input placeholder="计划名称（必填）" value={planName} onChange={setPlanName} />
          <Input.TextArea placeholder="计划描述（可选）" value={planDesc} onChange={setPlanDesc} autoSize={{ minRows: 2, maxRows: 4 }} />
          <Select allowClear placeholder="执行环境（可选）" value={planEnv} onChange={setPlanEnv}
            options={envs.map((e) => ({ label: e.name, value: e.id }))} />
          <Select allowClear placeholder="客户端（不选=服务端执行）" value={planAgent} onChange={setPlanAgent}
            options={agents.map((a) => ({ label: a.name, value: a.id }))} />
          <div style={{ color: 'var(--color-text-2)' }}>
            已选 {selected.length} 条用例；执行时自动带项目初始化用例。
          </div>
        </Space>
      </Modal>

      <Drawer title={detailPlan ? `计划详情 · ${detailPlan.name}` : '计划详情'} visible={!!detailPlan}
        width={560} footer={null} onCancel={() => setDetailPlan(null)}>
        {detailPlan ? (
          <Space direction="vertical" style={{ width: '100%' }}>
            <div style={{ color: 'var(--color-text-2)' }}>
              环境：{detailPlan.environment_name || '服务端默认'} · 客户端：{detailPlan.agent_name || '服务端执行'} · 最近批次：
              {detailPlan.last_batch_id ? <a href={`/reports?batch_id=${detailPlan.last_batch_id}`} target="_blank" rel="noreferrer">#{detailPlan.last_batch_id}</a> : '-'}
            </div>
            {detailPlan.description && <div>{detailPlan.description}</div>}
            <Table rowKey="case_id" data={detailPlan.items || []} pagination={{ pageSize: 20 }}
              columns={[
                { title: 'ID', dataIndex: 'case_id', width: 80 },
                { title: '类型', dataIndex: 'case_kind', width: 90 },
                { title: '保存时的理由', render: (_: any, r: PlanItem) => reasonTags(r.reasons) },
              ]} />
          </Space>
        ) : null}
      </Drawer>

      <Modal title="编辑回归计划" visible={!!editPlan} onOk={saveEdit} onCancel={() => setEditPlan(null)} okText="保存">
        <Space direction="vertical" style={{ width: '100%' }}>
          <Input placeholder="计划名称" value={editName} onChange={setEditName} />
          <Input.TextArea placeholder="计划描述" value={editDesc} onChange={setEditDesc} autoSize={{ minRows: 2, maxRows: 4 }} />
          <Select allowClear placeholder="执行环境（不选=服务端默认）" value={editEnv} onChange={setEditEnv}
            options={envs.map((e) => ({ label: e.name, value: e.id }))} />
          <Select allowClear placeholder="客户端（不选=服务端执行）" value={editAgent} onChange={setEditAgent}
            options={agents.map((a) => ({ label: a.name, value: a.id }))} />
        </Space>
      </Modal>

      <Modal title="执行回归计划" visible={!!runPlan} onOk={doRun} confirmLoading={running}
        onCancel={() => setRunPlan(null)} okText="开始执行">
        {runPlan ? (
          <Space direction="vertical" style={{ width: '100%' }}>
            <div>将执行 <b>{(runPlan.items || []).length}</b> 条用例（失效用例会自动跳过）。</div>
            <Select allowClear placeholder="执行环境" value={runEnv} onChange={setRunEnv}
              options={envs.map((e) => ({ label: e.name, value: e.id }))} />
            <Select allowClear placeholder="客户端（不选=服务端执行）" value={runAgent} onChange={setRunAgent}
              options={agents.map((a) => ({ label: a.name, value: a.id }))} />
            <Checkbox checked={runInit} onChange={setRunInit}>自动带项目初始化用例</Checkbox>
          </Space>
        ) : null}
      </Modal>
    </Space>
  );
}

function FlakyPanel({ projectId, onChanged }: { projectId: number; onChanged: () => void }) {
  const [rows, setRows] = useState<Flaky[]>([]);
  const [envs, setEnvs] = useState<Env[]>([]);
  const [envId, setEnvId] = useState<number>();
  const [loading, setLoading] = useState(false);
  const [statusFilter, setStatusFilter] = useState('all');
  const [keyword, setKeyword] = useState('');

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

  const filtered = rows.filter((r) => {
    if (statusFilter !== 'all' && r.status !== statusFilter) return false;
    if (keyword && !r.name.toLowerCase().includes(keyword.toLowerCase()) && !String(r.case_id).includes(keyword)) return false;
    return true;
  });

  const confirmFlaky = (row: Flaky) => {
    Modal.confirm({
      title: `确认用例「${row.name}」不稳定？`,
      content: '将使用所选环境连续复跑 3 次；期间会创建真实执行批次，可能需要数分钟。',
      okText: '开始复跑',
      onOk: async () => {
        try {
          const res = await api.post(`/api/quality/flaky/${row.case_id}/confirm`, { environment_id: envId });
          Message.success(`复跑结果：${FLAKY_LABELS[res.data.status] || res.data.status}`);
          await load();
          onChanged();
        } catch (e: any) { Message.error(e?.response?.data?.detail || '确认失败'); }
      },
    });
  };

  const doQuarantine = (row: Flaky) => {
    Modal.confirm({
      title: `隔离用例「${row.name}」？`,
      content: '隔离后，自动展开的批量执行会跳过该用例；手工执行仍可运行。',
      okText: '隔离',
      onOk: async () => {
        await api.post(`/api/quality/flaky/${row.case_id}/quarantine`, { reason: '人工隔离' });
        await load(); onChanged();
      },
    });
  };

  const doRelease = async (row: Flaky) => {
    await api.post(`/api/quality/flaky/${row.case_id}/release`, {});
    await load(); onChanged();
  };

  return (
    <Space direction="vertical" style={{ width: '100%' }}>
      <Card>
        <Space wrap>
          <span>确认复跑环境：</span>
          <Select style={{ width: 220 }} value={envId} onChange={setEnvId}
            options={envs.map((e) => ({ label: e.name, value: e.id }))} />
          <Input allowClear style={{ width: 220 }} placeholder="搜索用例名称 / ID" value={keyword} onChange={setKeyword} />
          <Select style={{ width: 140 }} value={statusFilter} onChange={setStatusFilter}
            options={[{ label: '全部状态', value: 'all' }, ...Object.entries(FLAKY_LABELS).map(([value, label]) => ({ label, value }))]} />
          <Button onClick={load} loading={loading}>刷新</Button>
          <span style={{ color: 'var(--color-text-2)' }}>隔离只影响自动执行；手工执行仍可运行。</span>
        </Space>
      </Card>
      <Card title={`Flaky 候选 / 隔离（${filtered.length}${filtered.length !== rows.length ? ` / 共 ${rows.length}` : ''}）`}>
        <Table rowKey="case_id" data={filtered} loading={loading} pagination={{ pageSize: 20, showTotal: true }}
          noDataElement={<Empty description="暂无 Flaky 候选；执行积累样本后可在这里确认与隔离" />}
          columns={[
            { title: 'ID', dataIndex: 'case_id', width: 55 },
            { title: '用例', dataIndex: 'name', width: 170, ellipsis: true, tooltip: true },
            {
              title: '状态', dataIndex: 'status', width: 85,
              render: (s: string, r: Flaky) => (
                <Tag color={s === 'quarantined' ? 'red' : s === 'recovered' ? 'green' : 'orange'} title={r.reason || undefined}>
                  {FLAKY_LABELS[s] || s}
                </Tag>
              ),
            },
            { title: '样本', dataIndex: 'sample_runs', width: 60 },
            { title: '通过/失败', width: 90, render: (_: any, r: Flaky) => `${r.passed} / ${r.failed}` },
            { title: '通过率', width: 80, render: (_: any, r: Flaky) => r.pass_rate == null ? '-' : `${r.pass_rate}%` },
            { title: '连续通过', dataIndex: 'consecutive_passes', width: 90 },
            { title: '最近失败', width: 130, ellipsis: true, render: (_: any, r: Flaky) => fmtTime(r.last_failed_at) },
            {
              title: '操作', width: 180,
              render: (_: any, r: Flaky) => (
                <Space>
                  {r.status !== 'quarantined' && <Button size="mini" onClick={() => confirmFlaky(r)}>确认不稳定</Button>}
                  {r.status !== 'quarantined' && <Button size="mini" status="danger" onClick={() => doQuarantine(r)}>隔离</Button>}
                  {r.status === 'quarantined' && (
                    <Popconfirm title="解除隔离？" onOk={() => doRelease(r)}>
                      <Button size="mini">解除</Button>
                    </Popconfirm>
                  )}
                </Space>
              ),
            },
          ]} />
      </Card>
    </Space>
  );
}

function RequirementPanel({ projectId, onChanged }: { projectId: number; onChanged: () => void }) {
  const [reqs, setReqs] = useState<Req[]>([]);
  const [matrix, setMatrix] = useState<any>(null);
  const [sessions, setSessions] = useState<GenSession[]>([]);
  const [sessionId, setSessionId] = useState<string>();
  const [kind, setKind] = useState('all');
  const [name, setName] = useState('');
  const [loading, setLoading] = useState(false);

  // 编辑需求
  const [editReq, setEditReq] = useState<Req | null>(null);
  const [editName, setEditName] = useState('');
  const [editDesc, setEditDesc] = useState('');

  // 关联用例
  const [linkReq, setLinkReq] = useState<Req | null>(null);
  const [linked, setLinked] = useState<LinkedCase[]>([]);
  const [caseKeyword, setCaseKeyword] = useState('');
  const [caseOptions, setCaseOptions] = useState<CaseItem[]>([]);
  const [pickedCases, setPickedCases] = useState<number[]>([]);
  const [searching, setSearching] = useState(false);

  // 无来源用例批量关联
  const [unlinkedSelected, setUnlinkedSelected] = useState<number[]>([]);
  const [bulkReq, setBulkReq] = useState<number>();

  const load = useCallback(async () => {
    if (!projectId) return;
    setLoading(true);
    try {
      const [r1, r2] = await Promise.all([
        api.get('/api/quality/requirements', { params: { project_id: projectId } }),
        api.get('/api/quality/requirements/matrix', { params: { project_id: projectId, case_kind: kind } }),
      ]);
      setReqs(r1.data.items || []);
      setMatrix(r2.data);
    } finally { setLoading(false); }
  }, [kind, projectId]);

  useEffect(() => { load().catch(() => undefined); }, [load]);
  useEffect(() => {
    api.get('/api/gen/history', { params: { project_id: projectId, page_size: 50 } })
      .then((r) => setSessions(r.data.items || []))
      .catch(() => undefined);
  }, [projectId]);

  const importFps = async () => {
    if (!sessionId) { Message.warning('请先选择生成会话'); return; }
    try {
      const res = await api.post('/api/quality/requirements/import', {
        project_id: projectId, session_id: sessionId,
      });
      Message.success(`导入 ${res.data.created} 条，跳过 ${res.data.skipped} 条`);
      await load(); onChanged();
    } catch (e: any) { Message.error(e?.response?.data?.detail || '导入失败'); }
  };

  const createReq = async () => {
    if (!name.trim()) { Message.warning('请输入需求名称'); return; }
    try {
      await api.post('/api/quality/requirements', { project_id: projectId, name: name.trim() });
      setName(''); await load(); onChanged();
    } catch (e: any) { Message.error(e?.response?.data?.detail || '创建失败'); }
  };

  const openEdit = (r: Req) => { setEditReq(r); setEditName(r.name); setEditDesc(r.description || ''); };
  const saveEdit = async () => {
    if (!editReq) return;
    try {
      await api.patch(`/api/quality/requirements/${editReq.id}`, {
        name: editName.trim() || undefined, description: editDesc || undefined,
      });
      Message.success('已更新');
      setEditReq(null); await load();
    } catch (e: any) { Message.error(e?.response?.data?.detail || '更新失败'); }
  };
  const removeReq = async (r: Req) => {
    try {
      await api.delete(`/api/quality/requirements/${r.id}`);
      Message.success('已删除'); await load(); onChanged();
    } catch (e: any) { Message.error(e?.response?.data?.detail || '删除失败'); }
  };

  const openLink = async (r: Req) => {
    setLinkReq(r); setPickedCases([]); setCaseKeyword(''); setCaseOptions([]);
    try {
      const res = await api.get(`/api/quality/requirements/${r.id}/links`);
      setLinked(res.data.items || []);
    } catch { setLinked([]); }
  };

  useEffect(() => {
    if (!linkReq || !caseKeyword.trim()) { setCaseOptions([]); return; }
    const timer = setTimeout(() => {
      setSearching(true);
      api.get('/api/testcases/search', { params: { project_id: projectId, q: caseKeyword.trim(), size: 20 } })
        .then((r) => setCaseOptions(r.data.items || []))
        .catch(() => setCaseOptions([]))
        .finally(() => setSearching(false));
    }, 300);
    return () => clearTimeout(timer);
  }, [caseKeyword, linkReq, projectId]);

  const addLinks = async () => {
    if (!linkReq || !pickedCases.length) { Message.warning('请选择要关联的用例'); return; }
    await api.post(`/api/quality/requirements/${linkReq.id}/links`, { case_ids: pickedCases });
    setPickedCases([]);
    const res = await api.get(`/api/quality/requirements/${linkReq.id}/links`);
    setLinked(res.data.items || []);
    await load(); onChanged();
    Message.success('已关联');
  };

  const unlink = async (caseId: number) => {
    if (!linkReq) return;
    await api.delete(`/api/quality/requirements/${linkReq.id}/links/${caseId}`);
    setLinked((prev) => prev.filter((c) => c.case_id !== caseId));
    await load(); onChanged();
  };

  const bulkLink = async () => {
    if (!bulkReq) { Message.warning('请选择目标需求'); return; }
    if (!unlinkedSelected.length) { Message.warning('请先勾选无来源用例'); return; }
    await api.post(`/api/quality/requirements/${bulkReq}/links`, { case_ids: unlinkedSelected });
    Message.success(`已关联 ${unlinkedSelected.length} 条用例`);
    setUnlinkedSelected([]); setBulkReq(undefined);
    await load(); onChanged();
  };

  const unlinked = matrix?.unlinked_cases || [];

  return (
    <Space direction="vertical" style={{ width: '100%' }}>
      <Card>
        <Space wrap size="medium">
          <Space size={4}>
            <span style={{ color: 'var(--color-text-2)' }}>生成会话：</span>
            <Select showSearch allowClear style={{ width: 340 }} value={sessionId} onChange={setSessionId}
              placeholder="选择要导入功能点的生成历史"
              filterOption={(input, option) => String(option?.props?.children || '').toLowerCase().includes(input.toLowerCase())}
              options={sessions.map((s) => ({
                label: `${s.filename || s.id} · ${fmtTime(s.created_at)} · ${s.test_cases_count ?? 0} 条`,
                value: s.id,
              }))} />
            <Button onClick={importFps} disabled={!sessionId}>导入功能点</Button>
          </Space>
          <Space size={4}>
            <span style={{ color: 'var(--color-text-2)' }}>新建需求：</span>
            <Input style={{ width: 220 }} placeholder="需求名称" value={name} onChange={setName}
              onPressEnter={createReq} />
            <Button onClick={createReq} disabled={!name.trim()}>新建</Button>
          </Space>
          <Space size={4}>
            <span style={{ color: 'var(--color-text-2)' }}>类型：</span>
            <Select style={{ width: 130 }} value={kind} onChange={setKind}
              options={[{ label: '全部类型', value: 'all' }, { label: 'UI', value: 'ui' }, { label: '功能', value: 'functional' }, { label: '接口', value: 'api' }]} />
          </Space>
          <Button loading={loading} onClick={load}>刷新</Button>
        </Space>
      </Card>

      <Card title={`需求覆盖矩阵（${matrix?.requirements?.length ?? reqs.length}）`}>
        <Table rowKey="id" data={matrix?.requirements || reqs} loading={loading}
          pagination={{ pageSize: 20, showTotal: true }}
          noDataElement={<Empty description="还没有需求条目：可从上方生成会话导入功能点，或手工新建" />}
          columns={[
            { title: '需求', dataIndex: 'name', render: (_: any, r: Req) => (
              <Space direction="vertical" size={0}>
                <span>{r.name}</span>
                {r.description ? <span style={{ color: 'var(--color-text-3)', fontSize: 12 }}>{r.description}</span> : null}
              </Space>
            ) },
            { title: '关联用例', width: 90, render: (_: any, r: Req) => r.case_count ?? 0 },
            { title: '通过/失败', width: 110, render: (_: any, r: Req) => `${r.passed ?? 0} / ${r.failed ?? 0}` },
            {
              title: '覆盖', dataIndex: 'coverage', width: 110,
              render: (c: string) => (
                <Tag color={c === 'uncovered' ? 'red' : c === 'partial' ? 'orange' : 'green'}>
                  {COVERAGE_LABELS[c] || c || '-'}
                </Tag>
              ),
            },
            {
              title: '操作', width: 220,
              render: (_: any, r: Req) => (
                <Space>
                  <Button size="mini" onClick={() => openLink(r)}>关联用例</Button>
                  <Button size="mini" onClick={() => openEdit(r)}>编辑</Button>
                  <Popconfirm title={`删除需求「${r.name}」？关联关系会一并删除。`} onOk={() => removeReq(r)}>
                    <Button size="mini" status="danger">删除</Button>
                  </Popconfirm>
                </Space>
              ),
            },
          ]} />
      </Card>

      <Card title={`无来源用例（${unlinked.length}）`}>
        <Space wrap style={{ marginBottom: 12 }}>
          <span style={{ color: 'var(--color-text-2)' }}>批量关联到：</span>
          <Select allowClear style={{ width: 260 }} placeholder="选择需求" value={bulkReq} onChange={setBulkReq}
            options={reqs.map((r) => ({ label: r.name, value: r.id }))} />
          <Button type="primary" disabled={!bulkReq || !unlinkedSelected.length} onClick={bulkLink}>
            关联选中（{unlinkedSelected.length}）
          </Button>
          <span style={{ color: 'var(--color-text-3)' }}>勾选后可批量关联；单条关联在需求行的「关联用例」中操作。</span>
        </Space>
        <Table rowKey="case_id" data={unlinked} pagination={{ pageSize: 10, showTotal: true }}
          rowSelection={{ selectedRowKeys: unlinkedSelected, onChange: (keys) => setUnlinkedSelected(keys as number[]) }}
          noDataElement={<Empty description="所有用例均已关联需求" />}
          columns={[
            { title: 'ID', dataIndex: 'case_id', width: 80 },
            { title: '名称', dataIndex: 'name' },
            { title: '类型', dataIndex: 'case_kind', width: 90 },
          ]} />
      </Card>

      <Modal title="编辑需求" visible={!!editReq} onOk={saveEdit} onCancel={() => setEditReq(null)} okText="保存">
        <Space direction="vertical" style={{ width: '100%' }}>
          <Input placeholder="需求名称" value={editName} onChange={setEditName} />
          <Input.TextArea placeholder="需求描述" value={editDesc} onChange={setEditDesc} autoSize={{ minRows: 2, maxRows: 5 }} />
        </Space>
      </Modal>

      <Modal title={linkReq ? `关联用例 · ${linkReq.name}` : '关联用例'} visible={!!linkReq}
        onCancel={() => setLinkReq(null)} footer={
          <Space>
            <Button onClick={() => setLinkReq(null)}>关闭</Button>
            <Button type="primary" disabled={!pickedCases.length} onClick={addLinks}>添加选中（{pickedCases.length}）</Button>
          </Space>
        } style={{ width: 720 }}>
        <Space direction="vertical" style={{ width: '100%' }}>
          <div style={{ color: 'var(--color-text-2)' }}>已关联（{linked.length}）</div>
          <Table rowKey="case_id" data={linked} size="small" pagination={{ pageSize: 5 }}
            columns={[
              { title: 'ID', dataIndex: 'case_id', width: 70 },
              { title: '名称', dataIndex: 'name' },
              {
                title: '操作', width: 90,
                render: (_: any, r: LinkedCase) => (
                  <Button size="mini" status="danger" onClick={() => unlink(r.case_id)}>解除</Button>
                ),
              },
            ]} />
          <div style={{ color: 'var(--color-text-2)', marginTop: 8 }}>搜索并添加用例</div>
          <Input allowClear placeholder="输入用例名称搜索（至少 1 个字）" value={caseKeyword} onChange={setCaseKeyword} />
          <Table rowKey="id" data={caseOptions} loading={searching} size="small" pagination={{ pageSize: 5 }}
            rowSelection={{
              selectedRowKeys: pickedCases,
              onChange: (keys) => setPickedCases(keys as number[]),
              checkboxProps: (record: any) => ({ disabled: linked.some((c) => c.case_id === record.id) }),
            }}
            columns={[
              { title: 'ID', dataIndex: 'id', width: 70 },
              { title: '名称', dataIndex: 'name' },
              { title: '类型', dataIndex: 'case_kind', width: 80 },
            ]} />
          {!caseOptions.length && caseKeyword.trim() && !searching ? <Empty description="没有匹配的用例" /> : null}
        </Space>
      </Modal>
    </Space>
  );
}

export default function QualityPage() {
  const [projects, setProjects] = useState<Project[]>([]);
  const [projectId, setProjectId] = useState<number>();
  const [refreshKey, setRefreshKey] = useState(0);

  useEffect(() => {
    api.get('/api/projects/').then((r) => {
      const list = Array.isArray(r.data) ? r.data : (r.data.items || []);
      setProjects(list);
      if (list.length) setProjectId(list[0].id);
    }).catch(() => undefined);
  }, []);

  const refreshOverview = useCallback(() => setRefreshKey((k) => k + 1), []);

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
        <>
          <OverviewCards projectId={projectId} refreshKey={refreshKey} />
          <Tabs type="rounded" destroyOnHide>
            <Tabs.TabPane key="impact" title="回归计划">
              <ImpactPanel projectId={projectId} onChanged={refreshOverview} />
            </Tabs.TabPane>
            <Tabs.TabPane key="flaky" title="Flaky 治理">
              <FlakyPanel projectId={projectId} onChanged={refreshOverview} />
            </Tabs.TabPane>
            <Tabs.TabPane key="requirements" title="需求追溯">
              <RequirementPanel projectId={projectId} onChanged={refreshOverview} />
            </Tabs.TabPane>
          </Tabs>
        </>
      ) : <Empty description="请选择项目" />}
    </div>
  );
}
