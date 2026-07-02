import React, { useEffect, useState, useCallback, useRef } from 'react';
import { useHistory } from 'react-router-dom';
import { Button, Modal, Form, Message, Popconfirm, Select, Space, Tag, Switch, Checkbox } from '@arco-design/web-react';
import { IconEdit, IconDelete, IconPlayArrow, IconStar, IconStarFill, IconBug } from '@arco-design/web-react/icon';
import axios from 'axios';
import { apiGet, apiRequest } from '@/utils/apiRequest';
import useLocale from '@/utils/useLocale';
import logger from '@/utils/logger';
import { TestCase, Module, Project, Environment, Step } from './types';
import useTestCaseData from './hooks/useTestCaseData';
import ModuleTree from './components/ModuleTree';
import TestCaseTable from './components/TestCaseTable';
import TestCaseEditor from './components/TestCaseEditor';
import ModuleEditor from './components/ModuleEditor';
import BatchMoveCopyModal from './components/BatchMoveCopyModal';
import EnvironmentManager from './components/EnvironmentManager';
import styles from './index.module.less';

const TestCases: React.FC = () => {
  const t = useLocale();
  const history = useHistory();
  const [projects, setProjects] = useState<Project[]>([]);
  const [selectedProject, setSelectedProject] = useState<number | null>(null);
  const [selectedModuleId, setSelectedModuleId] = useState<number | null>(null);
  const [visible, setVisible] = useState(false);
  const [editingCase, setEditingCase] = useState<TestCase | null>(null);
  const [form] = Form.useForm();
  const [steps, setSteps] = useState<Step[]>([]);
  const [moduleVisible, setModuleVisible] = useState(false);
  const [moduleForm] = Form.useForm();
  const [editingModule, setEditingModule] = useState<Module | null>(null);
  const [selectedRowKeys, setSelectedRowKeys] = useState<number[]>([]);
  const [batchModalVisible, setBatchModalVisible] = useState(false);
  const [batchAction, setBatchAction] = useState<'move' | 'copy'>('move');
  const [targetProjectId, setTargetProjectId] = useState<number | null>(null);
  const [targetModuleId, setTargetModuleId] = useState<number | null>(null);
  const [targetModules, setTargetModules] = useState<Module[]>([]);
  const [batchSubmitting, setBatchSubmitting] = useState(false);
  const [searchQuery, setSearchQuery] = useState('');
  const [submittedQuery, setSubmittedQuery] = useState('');
  const [agents, setAgents] = useState<{ name: string; status: string }[]>([]);
  const [selectedAgent, setSelectedAgent] = useState('');
  const [envManageVisible, setEnvManageVisible] = useState(false);
  const [envFormVisible, setEnvFormVisible] = useState(false);
  const [editingEnv, setEditingEnv] = useState<Environment | null>(null);
  const [envForm] = Form.useForm();
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);
  const [initCaseFilter, setInitCaseFilter] = useState<'all' | 'init' | 'normal'>('all');
  const [initCases, setInitCases] = useState<TestCase[]>([]);
  const [batchRunVisible, setBatchRunVisible] = useState(false);
  const [batchRunIncludeInit, setBatchRunIncludeInit] = useState(true);
  const [batchRunInitCaseIds, setBatchRunInitCaseIds] = useState<number[]>([]);
  const [batchRunLoading, setBatchRunLoading] = useState(false);
  const batchRunModeRef = useRef<'server' | 'client'>('server');

  const { data, total, loading, modules, moduleTree, environments, selectedEnvironment, setSelectedEnvironment, fetchData, fetchModules, fetchEnvironments } = useTestCaseData(selectedProject, selectedModuleId, page, pageSize, submittedQuery);

  useEffect(() => {
    apiGet<Project[]>('/api/projects/')
      .then((data) => setProjects(data || []))
      .catch((e) => {
        logger.error('Failed to load agents:', e);
      });
  }, []);
  useEffect(() => {
    apiRequest<{ name: string; status: string }[]>(
      { method: 'GET', url: '/api/agents' },
      { showSuccess: false, showError: false }
    )
      .then((res) => {
        const online = (Array.isArray(res) ? res : []).filter((a) => a.status === 'online');
        setAgents(online);
        if (online.length > 0) setSelectedAgent(online[0].name);
      })
      .catch((e) => {
        logger.error('Failed to load agents:', e);
      });
  }, []);

  const fetchInitCases = useCallback(async (projectId: number) => {
    try {
      const res = await axios.get('/api/testcases/init-cases', { params: { project_id: projectId } });
      setInitCases(res.data || []);
      setBatchRunInitCaseIds((res.data || []).map((c: TestCase) => c.id));
    } catch { setInitCases([]); }
  }, []);

  useEffect(() => {
    if (selectedProject) fetchInitCases(selectedProject);
    else setInitCases([]);
  }, [selectedProject, fetchInitCases]);

  const handleToggleInit = async (caseId: number, isInit: boolean) => {
    try {
      await axios.put(`/api/testcases/${caseId}/toggle-init`, { is_init: isInit });
      Message.success(isInit ? t['init.case.mark'] : t['init.case.unmark']);
      fetchData();
      if (selectedProject) fetchInitCases(selectedProject);
    } catch (e: unknown) { const err = e as { response?: { data?: { detail?: string } } }; Message.error(err.response?.data?.detail || t['operate.failed']); }
  };

  const handleProjectChange = (val: number) => { setSelectedProject(val); setSelectedModuleId(null); setPage(1); setSelectedRowKeys([]); setSearchQuery(''); setSubmittedQuery(''); setInitCaseFilter('all'); };
  const handleEnvironmentChange = (val: number) => setSelectedEnvironment(val);
  const openCreateEnv = () => { setEditingEnv(null); envForm.resetFields(); envForm.setFieldsValue({ browser: 'chromium', headless: true }); setEnvFormVisible(true); };
  const openEditEnv = (env: Environment) => { setEditingEnv(env); envForm.resetFields(); envForm.setFieldsValue(env); setEnvFormVisible(true); };
  const openEnvManage = () => setEnvManageVisible(true);
  const handleEnvSubmit = async () => { const values = await envForm.validate(); try { if (editingEnv) { await axios.put(`/api/environments/${editingEnv.id}`, values); Message.success(t['environment.update_success']); } else { await axios.post(`/api/projects/${selectedProject}/environments`, values); Message.success(t['environment.create_success']); } setEnvFormVisible(false); fetchEnvironments(); } catch (e: unknown) { const err = e as { response?: { data?: { detail?: string } } }; Message.error(err.response?.data?.detail || t['operate.failed']); } };
  const handleDeleteEnv = async (id: number) => { try { await axios.delete(`/api/environments/${id}`); Message.success(t['environment.delete_success']); fetchEnvironments(); } catch (e: unknown) { const err = e as { response?: { data?: { detail?: string } } }; Message.error(err.response?.data?.detail || t['operate.failed']); } };
  const handleSetDefaultEnv = async (id: number) => { try { await axios.put(`/api/environments/${id}/default`); Message.success(t['environment.set_default_success']); fetchEnvironments(); } catch (e: unknown) { const err = e as { response?: { data?: { detail?: string } } }; Message.error(err.response?.data?.detail || t['operate.failed']); } };
  const openCreate = () => { setEditingCase(null); form.resetFields(); if (selectedModuleId) form.setFieldsValue({ module_id: selectedModuleId }); setSteps([{ step_order: 1, description: '', parsed_result: '', retry_max: 0, retry_delay: 1.0 }]); setVisible(true); };
  const openEdit = (tc: TestCase) => { setEditingCase(tc); form.setFieldsValue({ name: tc.name, description: tc.description, module_id: tc.module_id }); setSteps(tc.steps && tc.steps.length ? tc.steps.map((s, i) => ({ step_order: i + 1, description: s.description, parsed_result: s.parsed_result || '', retry_max: s.retry_max ?? 0, retry_delay: s.retry_delay ?? 1.0 })) : [{ step_order: 1, description: '', parsed_result: '', retry_max: 0, retry_delay: 1.0 }]); setVisible(true); };
  const handleSubmit = async () => { try { const values = await form.validate(); const payload = { project_id: selectedProject, module_id: values.module_id || null, name: values.name, description: values.description, steps: steps.filter((s) => s.description.trim()).map((s) => ({ step_order: s.step_order, description: s.description, parsed_result: s.parsed_result || '', retry_max: s.retry_max ?? 0, retry_delay: s.retry_delay ?? 1.0 })) }; if (editingCase) { await axios.put(`/api/testcases/${editingCase.id}`, payload); Message.success(t['update.success']); } else { await axios.post('/api/testcases/', payload); Message.success(t['create.success']); } setVisible(false); fetchData(); } catch (e: unknown) { const err = e as { response?: { data?: { detail?: string } } }; if (err.response?.data?.detail) Message.error(err.response.data.detail); } };
  const handleDelete = async (id: number) => { try { await axios.delete(`/api/testcases/${id}`); Message.success(t['deleted']); fetchData(); } catch (err: unknown) { const e = err as { response?: { data?: { detail?: string } } }; Message.error(e?.response?.data?.detail || '操作失败'); } };
  const handleBatchDelete = async () => { if (selectedRowKeys.length === 0) return; try { await Promise.all(selectedRowKeys.map((id) => axios.delete(`/api/testcases/${id}`))); Message.success(t['delete.batch'].replace('{count}', String(selectedRowKeys.length))); setSelectedRowKeys([]); fetchData(); } catch (e: unknown) { const err = e as { response?: { data?: { detail?: string } } }; Message.error(err.response?.data?.detail || t['operate.failed']); } };
  const handleRun = async (id: number) => { try { const params: { environment_id?: number } = {}; if (selectedEnvironment) params.environment_id = selectedEnvironment; await axios.post(`/api/testcases/${id}/run`, null, { params }); Message.success(t['run.triggered']); } catch (e: unknown) { const err = e as { response?: { data?: { detail?: string } } }; Message.error(err.response?.data?.detail || t['run.failed']); } };
  const handleRunClient = async (id: number) => { try { const params: { agent_name?: string } = {}; if (selectedAgent) params.agent_name = selectedAgent; const res = await axios.post(`/api/testcases/${id}/run-client`, null, { params }); Message.success(t['client.run.triggered'].replace('{agent}', res.data.agent || 'agent')); } catch (e: unknown) { const err = e as { response?: { data?: { detail?: string }; agent?: string } }; Message.error(err.response?.data?.detail || t['run.failed']); } };
  const handleRunDebug = async (id: number) => { try { const res = await axios.post(`/api/testcases/${id}/run-debug`); const { run_id } = res.data; if (run_id) { Message.success('调试运行已触发'); history.push(`/run-debug?runId=${run_id}`); } else { Message.error('未获取到运行ID'); } } catch (e: unknown) { const err = e as { response?: { data?: { detail?: string } } }; Message.error(err.response?.data?.detail || '调试运行失败'); } };
  const openBatchModal = (action: 'move' | 'copy') => { setBatchAction(action); setTargetProjectId(null); setTargetModuleId(null); setTargetModules([]); setBatchModalVisible(true); };
  const handleBatchTargetProjectChange = (val: number) => { setTargetProjectId(val); setTargetModuleId(null); axios.get(`/api/projects/${val}/modules`).then((res) => { setTargetModules(res.data || []); }).catch((err) => Message.error(err?.response?.data?.detail || '操作失败')); };
  const handleBatchAction = async () => { if (!targetProjectId) { Message.error(t['select.project']); return; } setBatchSubmitting(true); try { const url = batchAction === 'move' ? '/api/testcases/batch-move' : '/api/testcases/batch-copy'; await axios.post(url, { case_ids: selectedRowKeys, project_id: targetProjectId, module_id: targetModuleId }); Message.success(batchAction === 'move' ? t['update.success'] : t['create.success']); setBatchModalVisible(false); setSelectedRowKeys([]); fetchData(); } catch (e: unknown) { const err = e as { response?: { data?: { detail?: string } } }; Message.error(err.response?.data?.detail || t['operate.failed']); } finally { setBatchSubmitting(false); } };
  const openModuleModal = (mod?: Module) => { setEditingModule(mod || null); moduleForm.resetFields(); if (mod) moduleForm.setFieldsValue(mod); setModuleVisible(true); };
  const handleModuleSubmit = async () => { const values = await moduleForm.validate(); try { if (editingModule) { await axios.put(`/api/modules/${editingModule.id}`, values); Message.success(t['update.success']); } else { await axios.post(`/api/projects/${selectedProject}/modules`, { ...values, project_id: selectedProject }); Message.success(t['create.success']); } setModuleVisible(false); fetchModules(); } catch (e: unknown) { const err = e as { response?: { data?: { detail?: string } } }; Message.error(err.response?.data?.detail || t['operate.failed']); } };
  const handleModuleDelete = async (id: number) => { try { await axios.delete(`/api/modules/${id}`); Message.success(t['deleted']); if (selectedModuleId === id) setSelectedModuleId(null); fetchModules(); } catch (e: unknown) { const err = e as { response?: { status?: number; data?: { detail?: string } } }; if (err.response?.status === 409) { Message.error(err.response.data?.detail || t['cannot.delete.module']); } else { Message.error(t['delete.failed']); } } };
  const handleRunModule = async (id: number) => { try { const params: { environment_id?: number } = {}; if (selectedEnvironment) params.environment_id = selectedEnvironment; await axios.post(`/api/testcases/module/${id}/run`, null, { params }); Message.success(t['run.triggered']); } catch (e: unknown) { const err = e as { response?: { data?: { detail?: string } } }; Message.error(err.response?.data?.detail || t['run.failed']); } };
  const handleRunAll = async () => { if (!selectedProject) return; try { const params: { environment_id?: number } = {}; if (selectedEnvironment) params.environment_id = selectedEnvironment; await axios.post(`/api/testcases/project/${selectedProject}/run`, null, { params }); Message.success(t['run.triggered']); } catch (e: unknown) { const err = e as { response?: { data?: { detail?: string } } }; Message.error(err.response?.data?.detail || t['run.failed']); } };

  const handleSelectModule = (id: number | null, resetPage?: boolean) => { setSelectedModuleId(id); if (resetPage) setPage(1); };

  const doBatchRun = async (initCaseIds: number[]) => {
    try {
      const payload: { case_ids: number[]; environment_id?: number; init_case_ids?: number[] } = { case_ids: selectedRowKeys };
      if (selectedEnvironment) payload.environment_id = selectedEnvironment;
      if (batchRunIncludeInit && initCaseIds.length > 0) payload.init_case_ids = initCaseIds;
      await axios.post('/api/testcases/batch-run', payload);
      Message.success(t['run.triggered']);
      setBatchRunVisible(false);
    } catch (e: unknown) {
      const err = e as { response?: { data?: { detail?: string } } };
      Message.error(err.response?.data?.detail || t['run.failed']);
    }
  };

  const doBatchRunClient = async (initCaseIds: number[]) => {
    try {
      if (!selectedAgent) { Message.warning(t['select.agent']); return; }
      const payload: { case_ids: number[]; agent_name: string; init_case_ids?: number[] } = { case_ids: selectedRowKeys, agent_name: selectedAgent };
      if (batchRunIncludeInit && initCaseIds.length > 0) payload.init_case_ids = initCaseIds;
      await axios.post('/api/testcases/batch-run-client', payload);
      Message.success(t['run.triggered']);
      setBatchRunVisible(false);
    } catch (e: unknown) {
      const err = e as { response?: { data?: { detail?: string } } };
      Message.error(err.response?.data?.detail || t['run.failed']);
    }
  };

  const openBatchRunDialog = (mode: 'server' | 'client') => {
    if (selectedRowKeys.length === 0) return;
    setBatchRunIncludeInit(true);
    setBatchRunInitCaseIds(initCases.map((c) => c.id));
    setBatchRunLoading(false);
    setBatchRunVisible(true);
    // 通过 ref 暂存运行模式，避免污染 window 全局命名空间
    batchRunModeRef.current = mode;
  };

  const handleBatchRunSubmit = async () => {
    setBatchRunLoading(true);
    try {
      const mode = batchRunModeRef.current;
      if (mode === 'client') await doBatchRunClient(batchRunInitCaseIds);
      else await doBatchRun(batchRunInitCaseIds);
    } finally {
      setBatchRunLoading(false);
    }
  };

  const columns = [
    { title: 'ID', dataIndex: 'project_case_number', width: 60 },
    {
      title: t['name'], dataIndex: 'name', render: (name: string, record: TestCase) => (
        <Space>
          {record.is_init && <Tag color="blue" size="small">{t['init.case']}</Tag>}
          <Button type="text" onClick={() => openEdit(record)}>{name}</Button>
        </Space>
      ),
    },
    { title: t['module'], dataIndex: 'module_id', width: 120, render: (mid: number | null) => { const m = modules.find((mod) => mod.id === mid); return m ? <Tag>{m.name}</Tag> : <Tag color="gray">--</Tag>; } },
    {
      title: t['description'], dataIndex: 'description', ellipsis: true,
    },
    {
      title: t['actions'], width: 480, render: (_: unknown, record: TestCase) => (
        <Space>
          <Button type="primary" size="small" icon={<IconPlayArrow />} onClick={() => handleRun(record.id)}>{t['run']}</Button>
          <Select value={selectedAgent} onChange={(val: string) => setSelectedAgent(val)} className={styles.agentSelect} size="mini">
            {(agents.length > 0 ? agents : [{ name: '', status: 'offline' }]).map(a => <Select.Option key={a.name} value={a.name} disabled={!a.name}>{a.name || t['select.agent']}</Select.Option>)}
          </Select>
          <Button type="outline" size="small" icon={<IconPlayArrow />} onClick={() => handleRunClient(record.id)}>{t['client']}</Button>
          <Button type="outline" size="small" icon={<IconBug />} onClick={() => handleRunDebug(record.id)}>调试运行</Button>
          <Button
            type="text"
            size="small"
            icon={record.is_init ? <IconStarFill className={styles.initIcon} /> : <IconStar />}
            onClick={() => handleToggleInit(record.id, !record.is_init)}
            aria-label={record.is_init ? '取消初始化' : '标记为初始化'}
          />
          <Button type="text" size="small" icon={<IconEdit />} onClick={() => openEdit(record)} aria-label="编辑用例" />
          <Popconfirm title={t['confirm.delete.item']} onOk={() => handleDelete(record.id)}>
            <Button type="text" size="small" status="danger" icon={<IconDelete />} aria-label="删除用例" />
          </Popconfirm>
        </Space>
      ),
    },
  ];

  // Filter data for init case filter
  const filteredData = data.filter((item: TestCase) => {
    if (initCaseFilter === 'init') return item.is_init;
    if (initCaseFilter === 'normal') return !item.is_init;
    return true;
  });

  const batchActions = selectedRowKeys.length > 0 ? <>
    <Popconfirm title={t['confirm.delete.item']} onOk={handleBatchDelete}><Button type="primary" status="danger" icon={<IconDelete />}>{t['delete.batch'].replace('{count}', String(selectedRowKeys.length))}</Button></Popconfirm>
    <Button type="outline" onClick={() => openBatchModal('move')}>{t['batch.move']}</Button>
    <Button type="outline" onClick={() => openBatchModal('copy')}>{t['batch.copy']}</Button>
    <Button type="primary" icon={<IconPlayArrow />} onClick={() => openBatchRunDialog('server')}>{t['batch.run.server']}</Button>
    <div className={styles.inlineFlex}>
      <Select value={selectedAgent} onChange={setSelectedAgent} className={styles.agentSelectWide} placeholder={t['select.agent']}>
        {(agents.length > 0 ? agents : [{ name: '', status: 'offline' }]).map(a => <Select.Option key={a.name} value={a.name} disabled={!a.name}>{a.name || t['select.agent']}</Select.Option>)}
      </Select>
      <Button type="outline" icon={<IconPlayArrow />} onClick={() => openBatchRunDialog('client')}>{t['batch.run.client']}</Button>
    </div>
  </> : null;

  const filterExtra = selectedProject ? (
    <Space>
      <Select
        value={initCaseFilter}
        onChange={(val: 'all' | 'init' | 'normal') => { setInitCaseFilter(val); setPage(1); }}
        className={styles.filterSelect}
        size="small"
      >
        <Select.Option value="all">全部用例</Select.Option>
        <Select.Option value="init">仅初始化用例</Select.Option>
        <Select.Option value="normal">仅普通用例</Select.Option>
      </Select>
      <Button size="small" onClick={() => window.open(`/api/testcases/export?project_id=${selectedProject}`, '_blank')}>
        导出 xlsx
      </Button>
      <input
        type="file"
        accept=".xlsx,.xls"
        style={{ display: 'none' }}
        id="import-file-input"
        onChange={async (e) => {
          const file = e.target.files?.[0];
          if (!file) return;
          try {
            const formData = new FormData();
            formData.append('file', file);
            const resp = await axios.post(`/api/testcases/import?project_id=${selectedProject}`, formData);
            Message.success(`已导入 ${resp.data?.created || 0} 条用例`);
            handleProjectChange(selectedProject);
          } catch (err: any) {
            Message.error('导入失败: ' + (err?.response?.data?.detail || '未知错误'));
          }
          e.target.value = '';
        }}
      />
      <Button size="small" onClick={() => document.getElementById('import-file-input')?.click()}>
        导入 xlsx
      </Button>
    </Space>
  ) : null;

  return (
    <>
      <style>{`.testcase-select .arco-select-view { background-color: var(--color-fill-2) !important; } .arco-tree-node { position: relative; } .arco-tree-node-title { width: 100%; padding-right: 0 !important; } .arco-tree-node-title .module-node-title { display: flex; align-items: center; justify-content: space-between; flex: 1; min-width: 0; } .step-row.drag-over { background: var(--color-primary-light-1); border-radius: 4px; } .step-row { cursor: default; }`}</style>
      <div className={styles.layout}>
        <ModuleTree projects={projects} selectedProject={selectedProject} onProjectChange={handleProjectChange} selectedEnvironment={selectedEnvironment} environments={environments} onEnvironmentChange={handleEnvironmentChange} modules={modules} moduleTree={moduleTree} selectedModuleId={selectedModuleId} onSelectModule={handleSelectModule} onCreateModule={() => openModuleModal()} onEditModule={openModuleModal} onDeleteModule={(id, name) => Modal.confirm({ title: t['confirm.delete'], content: t['confirm.delete.module'].replace('{name}', name), onOk: () => handleModuleDelete(id) })} onRunModule={handleRunModule} onRunAll={handleRunAll} t={t} openCreateEnv={openCreateEnv} openEnvManage={openEnvManage} />
        <TestCaseTable data={filteredData} loading={loading} total={total} page={page} pageSize={pageSize} columns={columns} selectedRowKeys={selectedRowKeys} onSelectionChange={setSelectedRowKeys} onPageChange={(p, ps) => { setPage(p); setPageSize(ps); }} searchQuery={searchQuery} onSearchChange={setSearchQuery} onSearch={(v) => { setPage(1); setSubmittedQuery(v); }} onClearSearch={() => { setSearchQuery(''); setSubmittedQuery(''); }} batchActions={batchActions} onCreate={openCreate} canCreate={!!selectedProject} filterExtra={filterExtra} t={t} />
        <TestCaseEditor visible={visible} editingCase={editingCase} onCancel={() => setVisible(false)} onSubmit={handleSubmit} modules={modules} projectId={selectedProject} t={t} form={form} steps={steps} setSteps={setSteps} />
        <BatchMoveCopyModal visible={batchModalVisible} batchAction={batchAction} onCancel={() => setBatchModalVisible(false)} onSubmit={handleBatchAction} projects={projects} targetProjectId={targetProjectId} onTargetProjectChange={handleBatchTargetProjectChange} targetModuleId={targetModuleId} onTargetModuleChange={setTargetModuleId} targetModules={targetModules} submitting={batchSubmitting} t={t} />
        <ModuleEditor visible={moduleVisible} editingModule={editingModule} onCancel={() => setModuleVisible(false)} onSubmit={handleModuleSubmit} modules={modules} form={moduleForm} t={t} />
        <EnvironmentManager manageVisible={envManageVisible} onCloseManage={() => setEnvManageVisible(false)} onCreate={openCreateEnv} environments={environments} onEdit={openEditEnv} onDelete={handleDeleteEnv} onSetDefault={handleSetDefaultEnv} t={t} formVisible={envFormVisible} editingEnv={editingEnv} onCancelForm={() => { setEnvFormVisible(false); setEditingEnv(null); }} onSubmitForm={handleEnvSubmit} form={envForm} />

        {/* Batch Run Dialog */}
        <Modal
          title={t['batch.run.server']}
          visible={batchRunVisible}
          onCancel={() => setBatchRunVisible(false)}
          onOk={handleBatchRunSubmit}
          confirmLoading={batchRunLoading}
          okText={t['run']}
        >
          <div className={styles.modalCount}>
            <div className={styles.modalTitle}>{t['case.count'].replace('{count}', String(selectedRowKeys.length))}:
              {' '}{(data || []).filter((c: TestCase) => selectedRowKeys.includes(c.id)).map((c: TestCase) => c.name).join(', ')}
            </div>
          </div>

          {initCases.length > 0 && (
            <div className={styles.initCaseBox}>
              <div className={styles.initSwitchRow}>
                <Switch
                  checked={batchRunIncludeInit}
                  onChange={(v) => setBatchRunIncludeInit(v)}
                />
                <span className={styles.switchLabel}>{t['init.case.run_before']}</span>
              </div>
              {batchRunIncludeInit && (
                <div>
                  <div className={styles.initSelectHint}>{t['init.case.select']}:</div>
                  <Checkbox.Group
                    value={batchRunInitCaseIds}
                    onChange={(values) => setBatchRunInitCaseIds(values as number[])}
                    direction="vertical"
                  >
                    {initCases.map((c) => (
                      <Checkbox key={c.id} value={c.id}>{c.name}</Checkbox>
                    ))}
                  </Checkbox.Group>
                </div>
              )}
            </div>
          )}
          {initCases.length === 0 && (
            <div className={styles.initNoneText}>{t['init.case.none']}</div>
          )}
        </Modal>
      </div>
    </>
  );
};

export default TestCases;
