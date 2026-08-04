import React, { useCallback, useEffect, useRef, useState } from 'react';
import {
  Button,
  Checkbox,
  Drawer,
  Form,
  Input,
  Message,
  Modal,
  Popconfirm,
  Select,
  Space,
  Switch,
  Table,
  Tag,
} from '@arco-design/web-react';
import { IconDelete, IconDragDotVertical, IconEdit, IconPlayArrow, IconPlus } from '@arco-design/web-react/icon';
import axios from 'axios';
import { apiGet, apiRequest } from '@/utils/apiRequest';
import useLocale from '@/utils/useLocale';
import logger from '@/utils/logger';
import styles from '../index.module.less';

interface Project {
  id: number;
  name: string;
}

interface Environment {
  id: number;
  name: string;
  is_default?: boolean;
}

interface SuiteCaseItem {
  case_id: number;
  order_index: number;
  name?: string | null;
  module_id?: number | null;
}

interface TestSuite {
  id: number;
  project_id: number;
  name: string;
  description?: string | null;
  case_kind: string;
  case_count: number;
  cases: SuiteCaseItem[];
}

interface CaseOption {
  id: number;
  name: string;
  is_init?: boolean;
}

const SuitesPage: React.FC = () => {
  const t = useLocale();
  const [projects, setProjects] = useState<Project[]>([]);
  const [selectedProject, setSelectedProject] = useState<number | null>(null);
  const [caseKind, setCaseKind] = useState<'ui' | 'functional'>('ui');
  const [suites, setSuites] = useState<TestSuite[]>([]);
  const [loading, setLoading] = useState(false);
  const [environments, setEnvironments] = useState<Environment[]>([]);
  const [selectedEnvironment, setSelectedEnvironment] = useState<number | undefined>();
  const [agents, setAgents] = useState<{ name: string; status: string }[]>([]);
  const [selectedAgent, setSelectedAgent] = useState('');
  const [initCases, setInitCases] = useState<CaseOption[]>([]);

  const [editVisible, setEditVisible] = useState(false);
  const [editing, setEditing] = useState<TestSuite | null>(null);
  const [editName, setEditName] = useState('');
  const [editDesc, setEditDesc] = useState('');
  const [orderedCases, setOrderedCases] = useState<SuiteCaseItem[]>([]);
  const [allCases, setAllCases] = useState<CaseOption[]>([]);
  const [saving, setSaving] = useState(false);
  const dragIndexRef = useRef<number | null>(null);

  const [runVisible, setRunVisible] = useState(false);
  const [runSuite, setRunSuite] = useState<TestSuite | null>(null);
  const [runIncludeInit, setRunIncludeInit] = useState(true);
  const [runInitCaseIds, setRunInitCaseIds] = useState<number[]>([]);
  const [runLoading, setRunLoading] = useState(false);
  const runModeRef = useRef<'server' | 'client'>('server');

  const fetchSuites = useCallback(async (projectId: number, kind: string) => {
    setLoading(true);
    try {
      const res = await axios.get('/api/suites', { params: { project_id: projectId, case_kind: kind } });
      setSuites(res.data || []);
    } catch (e) {
      logger.error('Failed to load suites', e);
      setSuites([]);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    apiGet<Project[]>('/api/projects/')
      .then((data) => {
        const list = data || [];
        setProjects(list);
        if (list.length > 0) setSelectedProject(list[0].id);
      })
      .catch((e) => logger.error('Failed to load projects', e));
  }, []);

  useEffect(() => {
    apiRequest<{ name: string; status: string }[]>(
      { method: 'GET', url: '/api/agents' },
      { showSuccess: false, showError: false },
    )
      .then((res) => {
        const online = (Array.isArray(res) ? res : []).filter((a) => a.status === 'online');
        setAgents(online);
        if (online.length > 0) setSelectedAgent(online[0].name);
      })
      .catch((e) => logger.error('Failed to load agents', e));
  }, []);

  useEffect(() => {
    if (!selectedProject) {
      setSuites([]);
      setEnvironments([]);
      setInitCases([]);
      return;
    }
    fetchSuites(selectedProject, caseKind);
    axios
      .get(`/api/projects/${selectedProject}/environments`)
      .then((res) => {
        const envs: Environment[] = res.data || [];
        setEnvironments(envs);
        const def = envs.find((e) => e.is_default) || envs[0];
        setSelectedEnvironment(def?.id);
      })
      .catch(() => setEnvironments([]));
    axios
      .get('/api/testcases/init-cases', { params: { project_id: selectedProject, case_kind: caseKind } })
      .then((res) => {
        const list = res.data || [];
        setInitCases(list);
        setRunInitCaseIds(list.map((c: CaseOption) => c.id));
      })
      .catch(() => setInitCases([]));
  }, [selectedProject, caseKind, fetchSuites]);

  const loadProjectCases = async (projectId: number, kind: string) => {
    try {
      const res = await axios.get(`/api/testcases/project/${projectId}/testcases`, {
        params: { page: 1, size: 500, case_kind: kind },
      });
      const items = res.data?.items || res.data || [];
      setAllCases((Array.isArray(items) ? items : []).map((c: CaseOption) => ({ id: c.id, name: c.name })));
    } catch {
      setAllCases([]);
    }
  };

  const openCreate = async () => {
    if (!selectedProject) return;
    setEditing(null);
    setEditName('');
    setEditDesc('');
    setOrderedCases([]);
    await loadProjectCases(selectedProject, caseKind);
    setEditVisible(true);
  };

  const openEdit = async (suite: TestSuite) => {
    setEditing(suite);
    setEditName(suite.name);
    setEditDesc(suite.description || '');
    try {
      const res = await axios.get(`/api/suites/${suite.id}`);
      const detail: TestSuite = res.data;
      setOrderedCases(detail.cases || []);
    } catch {
      setOrderedCases(suite.cases || []);
    }
    await loadProjectCases(suite.project_id, suite.case_kind || caseKind);
    setEditVisible(true);
  };

  const toggleCaseInSuite = (caseId: number, checked: boolean) => {
    if (checked) {
      const opt = allCases.find((c) => c.id === caseId);
      setOrderedCases((prev) => [
        ...prev,
        { case_id: caseId, order_index: prev.length, name: opt?.name },
      ]);
    } else {
      setOrderedCases((prev) =>
        prev
          .filter((c) => c.case_id !== caseId)
          .map((c, i) => ({ ...c, order_index: i })),
      );
    }
  };

  const handleDragStart = (idx: number) => (e: React.DragEvent) => {
    dragIndexRef.current = idx;
    e.dataTransfer.effectAllowed = 'move';
  };
  const handleDragOver = (idx: number) => (e: React.DragEvent) => {
    e.preventDefault();
    e.currentTarget.classList.add('drag-over');
  };
  const handleDragLeave = (idx: number) => (e: React.DragEvent) => {
    e.currentTarget.classList.remove('drag-over');
  };
  const handleDrop = (idx: number) => (e: React.DragEvent) => {
    e.preventDefault();
    e.currentTarget.classList.remove('drag-over');
    const from = dragIndexRef.current;
    dragIndexRef.current = null;
    if (from === null || from === idx) return;
    setOrderedCases((prev) => {
      const next = [...prev];
      const [item] = next.splice(from, 1);
      next.splice(idx, 0, item);
      return next.map((c, i) => ({ ...c, order_index: i }));
    });
  };

  const handleSave = async () => {
    const name = editName.trim();
    if (!name) {
      Message.warning(t['suite.name']);
      return;
    }
    if (!selectedProject && !editing) return;
    setSaving(true);
    try {
      const caseIds = orderedCases.map((c) => c.case_id);
      if (editing) {
        await axios.put(`/api/suites/${editing.id}`, {
          name,
          description: editDesc,
          case_ids: caseIds,
        });
        Message.success(t['suite.saved']);
      } else {
        await axios.post('/api/suites', {
          project_id: selectedProject,
          name,
          description: editDesc,
          case_kind: caseKind,
          case_ids: caseIds,
        });
        Message.success(t['suite.created']);
      }
      setEditVisible(false);
      if (selectedProject) fetchSuites(selectedProject, caseKind);
    } catch (e: unknown) {
      const err = e as { response?: { data?: { detail?: string } } };
      Message.error(err.response?.data?.detail || '保存失败');
    } finally {
      setSaving(false);
    }
  };

  const handleDelete = async (id: number) => {
    try {
      await axios.delete(`/api/suites/${id}`);
      Message.success(t['suite.deleted']);
      if (selectedProject) fetchSuites(selectedProject, caseKind);
    } catch (e: unknown) {
      const err = e as { response?: { data?: { detail?: string } } };
      Message.error(err.response?.data?.detail || '删除失败');
    }
  };

  const openRun = (suite: TestSuite, mode: 'server' | 'client') => {
    if (!suite.case_count) {
      Message.warning('空用例集不可执行');
      return;
    }
    runModeRef.current = mode;
    setRunSuite(suite);
    setRunIncludeInit(true);
    setRunInitCaseIds(initCases.map((c) => c.id));
    setRunVisible(true);
  };

  const handleRunSubmit = async () => {
    if (!runSuite) return;
    setRunLoading(true);
    try {
      const initIds = runIncludeInit ? runInitCaseIds : [];
      const mode = runModeRef.current;
      if (mode === 'client') {
        if (!selectedAgent) {
          Message.warning(t['select.agent']);
          return;
        }
        await axios.post(`/api/suites/${runSuite.id}/run-client`, {
          agent_name: selectedAgent,
          environment_id: selectedEnvironment,
          init_case_ids: initIds,
        });
      } else {
        await axios.post(`/api/suites/${runSuite.id}/run`, {
          environment_id: selectedEnvironment,
          init_case_ids: initIds,
        });
      }
      Message.success(t['run.triggered']);
      setRunVisible(false);
    } catch (e: unknown) {
      const err = e as { response?: { data?: { detail?: string } } };
      Message.error(err.response?.data?.detail || t['run.failed']);
    } finally {
      setRunLoading(false);
    }
  };

  const selectedCaseIdSet = new Set(orderedCases.map((c) => c.case_id));

  const columns = [
    { title: 'ID', dataIndex: 'id', width: 70 },
    { title: t['name'], dataIndex: 'name', ellipsis: true },
    {
      title: t['suite.case_count']?.replace('{count}', '') || 'Cases',
      dataIndex: 'case_count',
      width: 100,
      render: (n: number) => (t['suite.case_count'] || '{count}').replace('{count}', String(n)),
    },
    {
      title: t['case.kind'],
      dataIndex: 'case_kind',
      width: 100,
      render: (k: string) => <Tag>{k}</Tag>,
    },
    {
      title: t['actions'],
      width: 320,
      render: (_: unknown, record: TestSuite) => (
        <Space>
          <Button type="primary" size="mini" icon={<IconPlayArrow />} onClick={() => openRun(record, 'server')}>
            {t['suite.run']}
          </Button>
          <Button type="outline" size="mini" icon={<IconPlayArrow />} onClick={() => openRun(record, 'client')}>
            {t['batch.run.client']}
          </Button>
          <Button type="text" size="mini" icon={<IconEdit />} onClick={() => openEdit(record)} />
          <Popconfirm title={t['confirm.delete.item']} onOk={() => handleDelete(record.id)}>
            <Button type="text" size="mini" status="danger" icon={<IconDelete />} />
          </Popconfirm>
        </Space>
      ),
    },
  ];

  return (
    <div className={styles.layout} style={{ display: 'block', padding: 16 }}>
      <style>{`.suite-case-row.drag-over { border-color: rgb(var(--primary-6)) !important; background: var(--color-fill-2); }`}</style>
      <Space style={{ marginBottom: 16 }} wrap>
        <Select
          placeholder={t['menu.projects']}
          style={{ width: 200 }}
          value={selectedProject ?? undefined}
          onChange={(v) => setSelectedProject(v)}
        >
          {projects.map((p) => (
            <Select.Option key={p.id} value={p.id}>{p.name}</Select.Option>
          ))}
        </Select>
        <Select value={caseKind} onChange={setCaseKind} style={{ width: 140 }}>
          <Select.Option value="ui">{t['menu.testcases.ui']}</Select.Option>
          <Select.Option value="functional">{t['menu.testcases.functional']}</Select.Option>
        </Select>
        <Select
          placeholder="Environment"
          style={{ width: 180 }}
          allowClear
          value={selectedEnvironment}
          onChange={setSelectedEnvironment}
        >
          {environments.map((e) => (
            <Select.Option key={e.id} value={e.id}>{e.name}</Select.Option>
          ))}
        </Select>
        <Select
          placeholder={t['select.agent']}
          style={{ width: 160 }}
          value={selectedAgent || undefined}
          onChange={setSelectedAgent}
        >
          {(agents.length > 0 ? agents : [{ name: '', status: 'offline' }]).map((a) => (
            <Select.Option key={a.name} value={a.name} disabled={!a.name}>
              {a.name || t['select.agent']}
            </Select.Option>
          ))}
        </Select>
        <Button type="primary" icon={<IconPlus />} disabled={!selectedProject} onClick={openCreate}>
          {t['suite.save']}
        </Button>
      </Space>

      <Table
        rowKey="id"
        loading={loading}
        columns={columns}
        data={suites}
        pagination={false}
        noDataElement={t['suite.empty']}
      />

      <Drawer
        width={520}
        title={editing ? t['suite.edit'] : t['suite.save']}
        visible={editVisible}
        onCancel={() => setEditVisible(false)}
        footer={
          <Space>
            <Button onClick={() => setEditVisible(false)}>{t['cancel'] || '取消'}</Button>
            <Button type="primary" loading={saving} onClick={handleSave}>保存</Button>
          </Space>
        }
      >
        <Form layout="vertical">
          <Form.Item label={t['suite.name']} required>
            <Input value={editName} onChange={setEditName} maxLength={255} />
          </Form.Item>
          <Form.Item label={t['description']}>
            <Input.TextArea value={editDesc} onChange={setEditDesc} autoSize={{ minRows: 2 }} />
          </Form.Item>
          <Form.Item label={t['suite.select.cases']}>
            <div style={{ maxHeight: 180, overflow: 'auto', border: '1px solid var(--color-border-2)', padding: 8 }}>
              {allCases.map((c) => (
                <div key={c.id} style={{ marginBottom: 4 }}>
                  <Checkbox
                    checked={selectedCaseIdSet.has(c.id)}
                    onChange={(checked) => toggleCaseInSuite(c.id, checked)}
                  >
                    {c.name}
                  </Checkbox>
                </div>
              ))}
            </div>
          </Form.Item>
          <Form.Item label={t['suite.reorder.hint']}>
            <div>
              {orderedCases.map((c, idx) => (
                <div
                  key={`${c.case_id}-${idx}`}
                  className="suite-case-row"
                  style={{
                    display: 'flex',
                    alignItems: 'center',
                    gap: 8,
                    padding: '6px 8px',
                    marginBottom: 4,
                    border: '1px solid var(--color-border-2)',
                    borderRadius: 4,
                  }}
                  onDragOver={handleDragOver(idx)}
                  onDragLeave={handleDragLeave(idx)}
                  onDrop={handleDrop(idx)}
                >
                  <span
                    draggable
                    onDragStart={handleDragStart(idx)}
                    style={{ cursor: 'grab', display: 'inline-flex' }}
                  >
                    <IconDragDotVertical />
                  </span>
                  <span style={{ flex: 1 }}>{idx + 1}. {c.name || `#${c.case_id}`}</span>
                  <Button
                    type="text"
                    size="mini"
                    status="danger"
                    icon={<IconDelete />}
                    onClick={() => toggleCaseInSuite(c.case_id, false)}
                  />
                </div>
              ))}
            </div>
          </Form.Item>
        </Form>
      </Drawer>

      <Modal
        title={t['suite.run']}
        visible={runVisible}
        onCancel={() => setRunVisible(false)}
        onOk={handleRunSubmit}
        confirmLoading={runLoading}
        okText={t['run']}
      >
        <div className={styles.modalCount}>
          {runSuite?.name} — {(t['suite.case_count'] || '{count}').replace('{count}', String(runSuite?.case_count || 0))}
        </div>
        {initCases.length > 0 && (
          <div className={styles.initCaseBox}>
            <div className={styles.initSwitchRow}>
              <Switch checked={runIncludeInit} onChange={setRunIncludeInit} />
              <span className={styles.switchLabel}>{t['init.case.run_before']}</span>
            </div>
            {runIncludeInit && (
              <Checkbox.Group
                value={runInitCaseIds}
                onChange={(values) => setRunInitCaseIds(values as number[])}
                direction="vertical"
              >
                {initCases.map((c) => (
                  <Checkbox key={c.id} value={c.id}>{c.name}</Checkbox>
                ))}
              </Checkbox.Group>
            )}
          </div>
        )}
      </Modal>
    </div>
  );
};

export default SuitesPage;
