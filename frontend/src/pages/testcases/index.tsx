import React, { useEffect, useState, useCallback, useRef } from 'react';
import { useHistory, useLocation } from 'react-router-dom';
import { Button, Modal, Form, Message, Popconfirm, Select, Space, Tag, Switch, Checkbox, Input, InputNumber } from '@arco-design/web-react';
import { IconEdit, IconDelete, IconPlayArrow, IconStar, IconStarFill, IconBug, IconImport, IconMessage } from '@arco-design/web-react/icon';
import axios from 'axios';
import { apiGet, apiRequest } from '@/utils/apiRequest';
import useLocale from '@/utils/useLocale';
import logger from '@/utils/logger';
import { TestCase, Module, Project, Environment, Step, CaseKind } from './types';
import useTestCaseData from './hooks/useTestCaseData';
import ModuleTree from './components/ModuleTree';
import TestCaseTable from './components/TestCaseTable';
import TestCaseEditor from './components/TestCaseEditor';
import ModuleEditor from './components/ModuleEditor';
import BatchMoveCopyModal from './components/BatchMoveCopyModal';
import EnvironmentManager from './components/EnvironmentManager';
import { toEnvServicesForm, serializeEnvServices } from './utils/envPayload';
import { hydrateStructuredFromDescription } from './utils/parseInstantStep';
import {
  serializeEnvHeaders, serializeEnvVariables, toEnvHeadersForm, toEnvVariablesForm,
} from './utils/envPayload';
import styles from './index.module.less';

interface TestCasesProps {
  caseKind?: CaseKind;
}

const TestCases: React.FC<TestCasesProps> = ({ caseKind = 'ui' }) => {
  const t = useLocale();
  const history = useHistory();
  const location = useLocation();
  const isApi = caseKind === 'api';
  const allowRowRun = caseKind === 'ui';
  const allowBatchRun = caseKind === 'ui' || caseKind === 'api';
  const [projects, setProjects] = useState<Project[]>([]);
  const [selectedProject, setSelectedProject] = useState<number | null>(null);
  const [selectedModuleId, setSelectedModuleId] = useState<number | null>(null);
  const [visible, setVisible] = useState(false);
  const [editingCase, setEditingCase] = useState<TestCase | null>(null);
  const [form] = Form.useForm();
  const [steps, setSteps] = useState<Step[]>([]);
  // 039：参数声明 + 数据集绑定
  const [parameters, setParameters] = useState<{ name: string; description?: string }[]>([]);
  const [datasetBinding, setDatasetBinding] = useState<{
    dataset_id: number | null;
    dataset_mode: 'sequential' | 'random' | 'loop';
    loop_count: number;
    fail_fast: boolean;
  }>({ dataset_id: null, dataset_mode: 'sequential', loop_count: 1, fail_fast: false });
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
  // 031（US10）：批次内复用会话（共享 httpx client，cookie 可见；默认关闭）
  const [batchRunSessionReuse, setBatchRunSessionReuse] = useState(false);
  // 033（US10）：批量运行器选项（用例间隔 / 首个失败即停止）
  const [batchRunDelayMs, setBatchRunDelayMs] = useState<number | undefined>(undefined);
  const [batchRunStopOnError, setBatchRunStopOnError] = useState(false);
  // 039：本次执行的数据集覆盖（不写回用例）
  const [batchDatasets, setBatchDatasets] = useState<{ id: number; name: string; rows: unknown[] }[]>([]);
  const [batchDatasetId, setBatchDatasetId] = useState<number | undefined>(undefined);
  const [batchDatasetMode, setBatchDatasetMode] = useState<'sequential' | 'random' | 'loop'>('sequential');
  const [batchLoopCount, setBatchLoopCount] = useState(1);
  const [batchDatasetFailFast, setBatchDatasetFailFast] = useState(false);
  const [batchRunInitCaseIds, setBatchRunInitCaseIds] = useState<number[]>([]);
  const [batchRunLoading, setBatchRunLoading] = useState(false);
  const batchRunModeRef = useRef<'server' | 'client'>('server');
  const [saveSuiteVisible, setSaveSuiteVisible] = useState(false);
  const [saveSuiteName, setSaveSuiteName] = useState('');
  const [saveSuiteLoading, setSaveSuiteLoading] = useState(false);

  const { data, total, loading, modules, moduleTree, environments, selectedEnvironment, setSelectedEnvironment, fetchData, fetchModules, fetchEnvironments } = useTestCaseData(selectedProject, selectedModuleId, page, pageSize, submittedQuery, caseKind);

  useEffect(() => {
    apiGet<Project[]>('/api/projects/')
      .then((data) => {
        const list = data || [];
        setProjects(list);
        const qp = new URLSearchParams(location.search).get('projectId');
        const pid = qp ? Number(qp) : NaN;
        if (!Number.isNaN(pid) && list.some((p) => p.id === pid)) {
          setSelectedProject(pid);
        }
      })
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
      const res = await axios.get('/api/testcases/init-cases', { params: { project_id: projectId, case_kind: caseKind } });
      setInitCases(res.data || []);
      setBatchRunInitCaseIds((res.data || []).map((c: TestCase) => c.id));
    } catch { setInitCases([]); }
  }, [caseKind]);

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
  const openCreateEnv = () => { setEditingEnv(null); envForm.resetFields(); envForm.setFieldsValue({ browser: 'chromium', headless: true, variables: [], headers: [], services_list: [] }); setEnvFormVisible(true); };
  const openEditEnv = (env: Environment) => {
    setEditingEnv(env);
    envForm.resetFields();
    envForm.setFieldsValue({
      ...env,
      variables: toEnvVariablesForm(env.variables),
      headers: toEnvHeadersForm(env.headers),
      // 031（US4）：多域名服务地址
      services_list: toEnvServicesForm((env as { services?: Record<string, string> }).services),
    });
    setEnvFormVisible(true);
  };
  const openEnvManage = () => setEnvManageVisible(true);
  const handleEnvSubmit = async () => {
    const values = await envForm.validate();
    // secret 项「未修改」时 value 保持 "******"，后端据此保留原值；has_value 等仅 UI 字段剔除
    const payload = {
      ...values,
      variables: serializeEnvVariables(values.variables),
      headers: serializeEnvHeaders(values.headers),
      // 031（US4）：服务列表 ↔ dict
      services: serializeEnvServices(values.services_list),
    };
    try { if (editingEnv) { await axios.put(`/api/environments/${editingEnv.id}`, payload); Message.success(t['environment.update_success']); } else { await axios.post(`/api/projects/${selectedProject}/environments`, payload); Message.success(t['environment.create_success']); } setEnvFormVisible(false); fetchEnvironments(); } catch (e: unknown) { const err = e as { response?: { data?: { detail?: string } } }; Message.error(err.response?.data?.detail || t['operate.failed']); }
  };
  const handleDeleteEnv = async (id: number) => { try { await axios.delete(`/api/environments/${id}`); Message.success(t['environment.delete_success']); fetchEnvironments(); } catch (e: unknown) { const err = e as { response?: { data?: { detail?: string } } }; Message.error(err.response?.data?.detail || t['operate.failed']); } };
  const handleSetDefaultEnv = async (id: number) => { try { await axios.put(`/api/environments/${id}/default`); Message.success(t['environment.set_default_success']); fetchEnvironments(); } catch (e: unknown) { const err = e as { response?: { data?: { detail?: string } } }; Message.error(err.response?.data?.detail || t['operate.failed']); } };
  const openCreate = () => { setEditingCase(null); form.resetFields(); if (selectedModuleId) form.setFieldsValue({ module_id: selectedModuleId }); setParameters([]); setDatasetBinding({ dataset_id: null, dataset_mode: 'sequential', loop_count: 1, fail_fast: false }); setSteps([{ step_order: 1, description: '', parsed_result: '', retry_max: 0, retry_delay: 1.0, cacheable: true, structured_step: caseKind === 'ui' ? { action: 'click' } : null }]); setVisible(true); };
  const openEdit = (tc: TestCase) => {
    setEditingCase(tc);
    setParameters(Array.isArray(tc.parameters) ? tc.parameters : []);
    setDatasetBinding({
      dataset_id: tc.dataset_id ?? null,
      dataset_mode: (tc.dataset_mode as 'sequential' | 'random' | 'loop') || 'sequential',
      loop_count: tc.loop_count || 1,
      fail_fast: Boolean(tc.fail_fast),
    });
    form.setFieldsValue({ name: tc.name, description: tc.description, module_id: tc.module_id });
    // UI 步骤：历史数据常只有 Instant description，structured_step 为空 → 输入框空白而预览有文案
    setSteps(
      tc.steps && tc.steps.length
        ? tc.steps.map((s, i) => {
            const rawStructured = (s as any).structured_step ?? null;
            const structured =
              caseKind === 'ui'
                ? hydrateStructuredFromDescription(s.description || '', rawStructured)
                : rawStructured;
            return {
              step_order: i + 1,
              description: s.description,
              parsed_result: s.parsed_result || '',
              retry_max: s.retry_max ?? 0,
              retry_delay: s.retry_delay ?? 1.0,
              healed_selector: s.healed_selector,
              learned_locator: s.learned_locator ?? null,
              structured_step: structured,
              cacheable: s.cacheable !== false,
            };
          })
        : [{ step_order: 1, description: '', parsed_result: '', retry_max: 0, retry_delay: 1.0, cacheable: true, structured_step: caseKind === 'ui' ? { action: 'click' } : null }],
    );
    setVisible(true);
  };

  // 034（US6）：/testcases/{kind}?case_id= → 直接打开编辑弹窗（对话引用 chip 跳转）
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const cid = Number(params.get('case_id'));
    if (!Number.isFinite(cid) || cid <= 0) return;
    window.history.replaceState({}, '', window.location.pathname);
    axios
      .get(`/api/testcases/${cid}`)
      .then((res) => {
        const tc = res.data;
        const kind = tc.case_kind || 'ui';
        if (kind !== caseKind) {
          window.location.replace(`/testcases/${kind === 'functional' ? 'functional' : 'ui'}?case_id=${cid}`);
          return;
        }
        openEdit(tc);
      })
      .catch(() => Message.error('用例不存在或无权访问'));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  const handleSubmit = async () => { try { const values = await form.validate(); const payload = { project_id: selectedProject, module_id: values.module_id || null, name: values.name, description: values.description, case_kind: caseKind, parameters: parameters.filter((p) => p.name.trim()).map((p) => ({ name: p.name.trim(), description: p.description || '' })), dataset_id: datasetBinding.dataset_id, dataset_mode: datasetBinding.dataset_mode, loop_count: datasetBinding.loop_count, fail_fast: datasetBinding.fail_fast, steps: steps.filter((s) => s.description.trim() || !!(s.structured_step && (s.structured_step as any).target_name)).map((s) => ({ step_order: s.step_order, description: s.description || '步骤', parsed_result: s.parsed_result || '', retry_max: s.retry_max ?? 0, retry_delay: s.retry_delay ?? 1.0, learned_locator: s.learned_locator ?? null, structured_step: s.structured_step ?? null, healed_selector: s.healed_selector, cacheable: s.cacheable !== false })) }; if (editingCase) { const { case_kind: _ck, ...updatePayload } = payload; await axios.put(`/api/testcases/${editingCase.id}`, updatePayload); Message.success(t['update.success']); } else { await axios.post('/api/testcases/', payload); Message.success(t['create.success']); } setVisible(false); fetchData(); } catch (e: unknown) { const err = e as { response?: { data?: { detail?: string } } }; if (err.response?.data?.detail) Message.error(err.response.data.detail); } };
  const handleDelete = async (id: number) => { try { await axios.delete(`/api/testcases/${id}`); Message.success(t['deleted']); fetchData(); } catch (err: unknown) { const e = err as { response?: { data?: { detail?: string } } }; Message.error(e?.response?.data?.detail || '操作失败'); } };
  const handleBatchDelete = async () => { if (selectedRowKeys.length === 0) return; try { await Promise.all(selectedRowKeys.map((id) => axios.delete(`/api/testcases/${id}`))); Message.success(t['delete.batch'].replace('{count}', String(selectedRowKeys.length))); setSelectedRowKeys([]); fetchData(); } catch (e: unknown) { const err = e as { response?: { data?: { detail?: string } } }; Message.error(err.response?.data?.detail || t['operate.failed']); } };
  const handleRun = async (id: number) => { try { const params: { environment_id?: number } = {}; if (selectedEnvironment) params.environment_id = selectedEnvironment; await axios.post(`/api/testcases/${id}/run`, null, { params }); Message.success(t['run.triggered']); } catch (e: unknown) { const err = e as { response?: { data?: { detail?: string } } }; Message.error(err.response?.data?.detail || t['run.failed']); } };
  const handleRunClient = async (id: number) => { try { const params: { agent_name?: string; environment_id?: number } = {}; if (selectedAgent) params.agent_name = selectedAgent; if (selectedEnvironment) params.environment_id = selectedEnvironment; const res = await axios.post(`/api/testcases/${id}/run-client`, null, { params }); Message.success(t['client.run.triggered'].replace('{agent}', res.data.agent || 'agent')); } catch (e: unknown) { const err = e as { response?: { data?: { detail?: string }; agent?: string } }; Message.error(err.response?.data?.detail || t['run.failed']); } };
  const handleRunDebug = async (id: number) => { try { const res = await axios.post(`/api/testcases/${id}/run-debug`); const { run_id } = res.data; if (run_id) { Message.success('调试运行已触发'); history.push(`/run-debug?runId=${run_id}`); } else { Message.error('未获取到运行ID'); } } catch (e: unknown) { const err = e as { response?: { data?: { detail?: string } } }; Message.error(err.response?.data?.detail || '调试运行失败'); } };
  const openBatchModal = (action: 'move' | 'copy') => { setBatchAction(action); setTargetProjectId(null); setTargetModuleId(null); setTargetModules([]); setBatchModalVisible(true); };
  const handleBatchTargetProjectChange = (val: number) => { setTargetProjectId(val); setTargetModuleId(null); axios.get(`/api/projects/${val}/modules`).then((res) => { setTargetModules(res.data || []); }).catch((err) => Message.error(err?.response?.data?.detail || '操作失败')); };
  const handleBatchAction = async () => { if (!targetProjectId) { Message.error(t['select.project']); return; } setBatchSubmitting(true); try { const url = batchAction === 'move' ? '/api/testcases/batch-move' : '/api/testcases/batch-copy'; await axios.post(url, { case_ids: selectedRowKeys, project_id: targetProjectId, module_id: targetModuleId }); Message.success(batchAction === 'move' ? t['update.success'] : t['create.success']); setBatchModalVisible(false); setSelectedRowKeys([]); fetchData(); } catch (e: unknown) { const err = e as { response?: { data?: { detail?: string } } }; Message.error(err.response?.data?.detail || t['operate.failed']); } finally { setBatchSubmitting(false); } };
  const openModuleModal = (mod?: Module) => { setEditingModule(mod || null); moduleForm.resetFields(); if (mod) moduleForm.setFieldsValue(mod); setModuleVisible(true); };
  const handleModuleSubmit = async () => { const values = await moduleForm.validate(); try { if (editingModule) { await axios.put(`/api/modules/${editingModule.id}`, values); Message.success(t['update.success']); } else { await axios.post(`/api/projects/${selectedProject}/modules`, { ...values, project_id: selectedProject }); Message.success(t['create.success']); } setModuleVisible(false); fetchModules(); } catch (e: unknown) { const err = e as { response?: { data?: { detail?: string } } }; Message.error(err.response?.data?.detail || t['operate.failed']); } };
  const handleModuleDelete = useCallback(async (id: number) => {
    try {
      await axios.delete(`/api/modules/${id}`);
      Message.success(t['deleted']);
      if (selectedModuleId === id) setSelectedModuleId(null);
      fetchModules();
    } catch (e: unknown) {
      const err = e as { response?: { status?: number; data?: { detail?: string } } };
      if (err.response?.status === 409) { Message.error(err.response.data?.detail || t['cannot.delete.module']); }
      else { Message.error(t['delete.failed']); }
    }
  }, [selectedModuleId, t]);
  // 039：批量执行弹窗打开时加载项目内 UI 数据集
  useEffect(() => {
    if (!batchRunVisible || !selectedProject || caseKind !== 'ui') return;
    axios
      .get('/api/api-test/datasets', { params: { project_id: selectedProject, purpose: 'ui' } })
      .then((res) => setBatchDatasets(Array.isArray(res.data) ? res.data : (res.data?.items || [])))
      .catch(() => setBatchDatasets([]));
  }, [batchRunVisible, selectedProject, caseKind]);
  const handleRunModule = async (id: number) => { try { const params: { environment_id?: number } = {}; if (selectedEnvironment) params.environment_id = selectedEnvironment; await axios.post(`/api/testcases/module/${id}/run`, null, { params }); Message.success(t['run.triggered']); } catch (e: unknown) { const err = e as { response?: { data?: { detail?: string } } }; Message.error(err.response?.data?.detail || t['run.failed']); } };
  const handleRunAll = async () => { if (!selectedProject) return; try { const params: { environment_id?: number } = {}; if (selectedEnvironment) params.environment_id = selectedEnvironment; await axios.post(`/api/testcases/project/${selectedProject}/run`, null, { params }); Message.success(t['run.triggered']); } catch (e: unknown) { const err = e as { response?: { data?: { detail?: string } } }; Message.error(err.response?.data?.detail || t['run.failed']); } };

  const handleSelectModule = (id: number | null, resetPage?: boolean) => { setSelectedModuleId(id); if (resetPage) setPage(1); };

  const doBatchRun = async (initCaseIds: number[]) => {
    try {
      const payload: {
        case_ids: number[];
        environment_id?: number;
        init_case_ids?: number[];
        init_policy: 'before_each';
        session_reuse?: boolean;
        delay_ms?: number;
        stop_on_error?: boolean;
        dataset_id?: number;
        dataset_mode?: string;
        loop_count?: number;
        dataset_fail_fast?: boolean;
      } = { case_ids: selectedRowKeys, init_policy: 'before_each' };
      if (selectedEnvironment) payload.environment_id = selectedEnvironment;
      if (batchRunIncludeInit && initCaseIds.length > 0) payload.init_case_ids = initCaseIds;
      // 031（US10）：开启后批次内共享 HTTP 客户端（登录一次，后续用例自动带 Cookie）
      if (batchRunSessionReuse) payload.session_reuse = true;
      // 033（US10）：运行器选项
      if (batchRunDelayMs && batchRunDelayMs > 0) payload.delay_ms = batchRunDelayMs;
      if (batchRunStopOnError) payload.stop_on_error = true;
      if (batchDatasetId) {
        payload.dataset_id = batchDatasetId;
        payload.dataset_mode = batchDatasetMode;
        payload.loop_count = batchLoopCount;
        payload.dataset_fail_fast = batchDatasetFailFast;
      }
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
      const payload: {
        case_ids: number[];
        agent_name: string;
        environment_id?: number;
        init_case_ids?: number[];
        init_policy: 'before_each';
        dataset_id?: number;
        dataset_mode?: string;
        loop_count?: number;
        dataset_fail_fast?: boolean;
      } = { case_ids: selectedRowKeys, agent_name: selectedAgent, init_policy: 'before_each' };
      if (selectedEnvironment) payload.environment_id = selectedEnvironment;
      if (batchRunIncludeInit && initCaseIds.length > 0) payload.init_case_ids = initCaseIds;
      if (batchDatasetId) {
        payload.dataset_id = batchDatasetId;
        payload.dataset_mode = batchDatasetMode;
        payload.loop_count = batchLoopCount;
        payload.dataset_fail_fast = batchDatasetFailFast;
      }
      await axios.post('/api/testcases/batch-run-client', payload);
      Message.success(t['run.triggered']);
      setBatchRunVisible(false);
    } catch (e: unknown) {
      const err = e as { response?: { data?: { detail?: string } } };
      Message.error(err.response?.data?.detail || t['run.failed']);
    }
  };

  const handleSaveAsSuite = async () => {
    if (!selectedProject) return;
    const name = saveSuiteName.trim();
    if (!name) { Message.warning(t['suite.name']); return; }
    if (selectedRowKeys.length === 0) return;
    setSaveSuiteLoading(true);
    try {
      await axios.post('/api/suites', {
        project_id: selectedProject,
        name,
        case_kind: caseKind,
        case_ids: selectedRowKeys,
      });
      Message.success(t['suite.created']);
      setSaveSuiteVisible(false);
      setSaveSuiteName('');
    } catch (e: unknown) {
      const err = e as { response?: { data?: { detail?: string } } };
      Message.error(err.response?.data?.detail || t['save.failed'] || '保存失败');
    } finally {
      setSaveSuiteLoading(false);
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
      title: t['name'], dataIndex: 'name', ellipsis: true, render: (name: string, record: TestCase) => (
        <div className={styles.nameCell}>
          {record.is_init && <Tag color="blue" size="small">{t['init.case']}</Tag>}
          <Button type="text" className={styles.nameBtn} onClick={() => openEdit(record)}>{name}</Button>
        </div>
      ),
    },
    { title: t['module'], dataIndex: 'module_id', width: 90, render: (mid: number | null) => { const m = modules.find((mod) => mod.id === mid); return m ? <Tag>{m.name}</Tag> : <Tag color="gray">--</Tag>; } },
    {
      title: t['description'], dataIndex: 'description', ellipsis: true, width: 160,
    },
    {
      title: t['actions'], render: (_: unknown, record: TestCase) => (
        <div className={styles.actionsCell}>
          {allowRowRun && (
            <>
              <Button type="primary" size="mini" icon={<IconPlayArrow />} onClick={() => handleRun(record.id)}>{t['run']}</Button>
              <Select value={selectedAgent} onChange={(val: string) => setSelectedAgent(val)} className={styles.agentSelectMini} size="mini">
                {(agents.length > 0 ? agents : [{ name: '', status: 'offline' }]).map(a => <Select.Option key={a.name} value={a.name} disabled={!a.name}>{a.name || t['select.agent']}</Select.Option>)}
              </Select>
              <Button type="outline" size="mini" icon={<IconPlayArrow />} onClick={() => handleRunClient(record.id)}>{t['client']}</Button>
              <Button type="outline" size="mini" icon={<IconBug />} onClick={() => handleRunDebug(record.id)}>调试</Button>
            </>
          )}
          <Button type="text" size="mini" icon={record.is_init ? <IconStarFill className={styles.initIcon} /> : <IconStar />}
            onClick={() => handleToggleInit(record.id, !record.is_init)}
            aria-label={record.is_init ? '取消初始化' : '标记为初始化'}
          />
          <Button type="text" size="mini" icon={<IconMessage />}
            onClick={() => { window.location.href = `/chat?case_id=${record.id}`; }}
            aria-label="在对话中打开" title="在对话中打开"
          />
          <Button type="text" size="mini" icon={<IconEdit />} onClick={() => openEdit(record)} aria-label="编辑用例" />
          <Popconfirm title={t['confirm.delete.item']} onOk={() => handleDelete(record.id)}>
            <Button type="text" size="mini" status="danger" icon={<IconDelete />} aria-label="删除用例" />
          </Popconfirm>
        </div>
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
    <Button type="outline" onClick={() => { setSaveSuiteName(''); setSaveSuiteVisible(true); }}>{t['suite.save']}</Button>
    {allowBatchRun && (
      <>
        <Button type="primary" icon={<IconPlayArrow />} onClick={() => openBatchRunDialog('server')}>{t['batch.run.server']}</Button>
        {!isApi && (
          <div className={styles.inlineFlex}>
            <Select value={selectedAgent} onChange={setSelectedAgent} className={styles.agentSelectWide} placeholder={t['select.agent']}>
              {(agents.length > 0 ? agents : [{ name: '', status: 'offline' }]).map(a => <Select.Option key={a.name} value={a.name} disabled={!a.name}>{a.name || t['select.agent']}</Select.Option>)}
            </Select>
            <Button type="outline" icon={<IconPlayArrow />} onClick={() => openBatchRunDialog('client')}>{t['batch.run.client']}</Button>
          </div>
        )}
      </>
    )}
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
      <Button size="small" onClick={() => window.open(`/api/testcases/export?project_id=${selectedProject}&case_kind=${caseKind}`, '_blank')}>
        导出 xlsx
      </Button>
      <input
        type="file"
        accept=".xlsx,.xls"
        style={{ display: 'none' }}
        id={`import-file-input-${caseKind}`}
        onChange={async (e) => {
          const file = e.target.files?.[0];
          if (!file) return;
          try {
            const formData = new FormData();
            formData.append('file', file);
            const resp = await axios.post(`/api/testcases/import?project_id=${selectedProject}&case_kind=${caseKind}`, formData);
            Message.success(`已导入 ${resp.data?.created || 0} 条用例`);
            handleProjectChange(selectedProject);
          } catch (err: any) {
            Message.error('导入失败: ' + (err?.response?.data?.detail || '未知错误'));
          }
          e.target.value = '';
        }}
      />
      <Button size="small" onClick={() => document.getElementById(`import-file-input-${caseKind}`)?.click()}>
        导入 xlsx
      </Button>
    </Space>
  ) : null;

  const createButton = isApi ? (
    <Button type="primary" icon={<IconImport />} onClick={() => history.push('/api_test')}>
      {t['api_case.goto_import'] || '去接口测试页导入生成'}
    </Button>
  ) : undefined;

  return (
    <>
      <style>{`.testcase-select .arco-select-view { background-color: var(--color-fill-2) !important; } .arco-tree-node { position: relative; } .arco-tree-node-title { width: 100%; padding-right: 0 !important; } .arco-tree-node-title .module-node-title { display: flex; align-items: center; justify-content: space-between; flex: 1; min-width: 0; } .step-row.drag-over { border-color: rgb(var(--primary-6)) !important; box-shadow: 0 0 0 2px rgba(var(--primary-6), 0.12); }`}</style>
      <div className={styles.layout}>
        <ModuleTree projects={projects} selectedProject={selectedProject} onProjectChange={handleProjectChange} selectedEnvironment={selectedEnvironment} environments={environments} onEnvironmentChange={handleEnvironmentChange} modules={modules} moduleTree={moduleTree} selectedModuleId={selectedModuleId} onSelectModule={handleSelectModule} onCreateModule={() => openModuleModal()} onEditModule={openModuleModal} onDeleteModule={(id, name) => Modal.confirm({ title: t['confirm.delete'], content: t['confirm.delete.module'].replace('{name}', name), onOk: () => handleModuleDelete(id) })} onRunModule={allowRowRun ? handleRunModule : undefined} onRunAll={allowRowRun ? handleRunAll : undefined} allowRun={allowRowRun} t={t} openCreateEnv={openCreateEnv} openEnvManage={openEnvManage} />
        <TestCaseTable data={filteredData} loading={loading} total={total} page={page} pageSize={pageSize} columns={columns} selectedRowKeys={selectedRowKeys} onSelectionChange={setSelectedRowKeys} onPageChange={(p, ps) => { setPage(p); setPageSize(ps); }} searchQuery={searchQuery} onSearchChange={setSearchQuery} onSearch={(v) => { setPage(1); setSubmittedQuery(v); }} onClearSearch={() => { setSearchQuery(''); setSubmittedQuery(''); }} batchActions={batchActions} onCreate={openCreate} canCreate={!!selectedProject} createButton={createButton} filterExtra={filterExtra} t={t} />
        <TestCaseEditor
          visible={visible}
          editingCase={editingCase}
          onCancel={() => setVisible(false)}
          onSubmit={handleSubmit}
          modules={modules}
          projectId={selectedProject}
          t={t}
          form={form}
          steps={steps}
          setSteps={setSteps}
          caseKind={caseKind}
          parameters={parameters}
          setParameters={setParameters}
          datasetBinding={datasetBinding}
          setDatasetBinding={setDatasetBinding}
          onCompiledScriptCleared={() => {
            if (editingCase) {
              setEditingCase({
                ...editingCase,
                compiled_script: null,
                compiled_script_hash: null,
                compiled_at: null,
              });
            }
            fetchData();
          }}
        />
        <BatchMoveCopyModal visible={batchModalVisible} batchAction={batchAction} onCancel={() => setBatchModalVisible(false)} onSubmit={handleBatchAction} projects={projects} targetProjectId={targetProjectId} onTargetProjectChange={handleBatchTargetProjectChange} targetModuleId={targetModuleId} onTargetModuleChange={setTargetModuleId} targetModules={targetModules} submitting={batchSubmitting} t={t} />
        <ModuleEditor visible={moduleVisible} editingModule={editingModule} onCancel={() => setModuleVisible(false)} onSubmit={handleModuleSubmit} modules={modules} form={moduleForm} t={t} />
        <EnvironmentManager manageVisible={envManageVisible} onCloseManage={() => setEnvManageVisible(false)} onCreate={openCreateEnv} environments={environments} onEdit={openEditEnv} onDelete={handleDeleteEnv} onSetDefault={handleSetDefaultEnv} t={t} formVisible={envFormVisible} editingEnv={editingEnv} onCancelForm={() => { setEnvFormVisible(false); setEditingEnv(null); }} onSubmitForm={handleEnvSubmit} form={envForm} />

        <Modal
          title={t['suite.save']}
          visible={saveSuiteVisible}
          onCancel={() => setSaveSuiteVisible(false)}
          onOk={handleSaveAsSuite}
          confirmLoading={saveSuiteLoading}
        >
          <Form layout="vertical">
            <Form.Item label={t['suite.name']} required>
              <Input
                value={saveSuiteName}
                onChange={setSaveSuiteName}
                placeholder={t['suite.name']}
                maxLength={255}
              />
            </Form.Item>
            <div className={styles.modalCount}>
              {(t['suite.case_count'] || '{count}').replace('{count}', String(selectedRowKeys.length))}
            </div>
          </Form>
        </Modal>

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
          {/* 031（US10）：复用会话（接口用例批次内共享 Cookie） */}
          <div className={styles.initSwitchRow} style={{ marginTop: 12 }}>
            <Switch
              checked={batchRunSessionReuse}
              onChange={(v) => setBatchRunSessionReuse(v)}
            />
            <span className={styles.switchLabel}>{t['batch.session_reuse']}</span>
          </div>
          <div className={styles.initNoneText}>{t['batch.session_reuse.hint']}</div>
          {/* 033（US10）：运行器选项（用例间隔 / 首个失败即停止） */}
          <div className={styles.initSwitchRow} style={{ marginTop: 12 }}>
            <span className={styles.switchLabel}>{t['batch.delay_ms']}</span>
            <InputNumber
              min={0}
              max={60000}
              value={batchRunDelayMs}
              onChange={(v) => setBatchRunDelayMs(typeof v === 'number' ? v : undefined)}
              style={{ width: 140, marginLeft: 8 }}
              placeholder="0"
            />
          </div>
          <div className={styles.initSwitchRow} style={{ marginTop: 12 }}>
            <Switch
              checked={batchRunStopOnError}
              onChange={(v) => setBatchRunStopOnError(v)}
            />
            <span className={styles.switchLabel}>{t['batch.stop_on_error']}</span>
          </div>
          <div className={styles.initNoneText}>{t['batch.stop_on_error.hint']}</div>
          {/* 039：本次执行的数据集覆盖（不写回用例） */}
          {caseKind === 'ui' && (
            <>
              <div className={styles.initSwitchRow} style={{ marginTop: 12 }}>
                <span className={styles.switchLabel}>{t['param.dataset'] || '数据集（本次执行）'}</span>
                <Select
                  allowClear
                  style={{ width: 220, marginLeft: 8 }}
                  placeholder={t['param.dataset.select'] || '不绑定（按用例配置）'}
                  value={batchDatasetId}
                  onChange={(v) => setBatchDatasetId(v as number | undefined)}
                  options={batchDatasets.map((d) => ({
                    label: `${d.name}（${d.rows.length} 行）`,
                    value: d.id,
                  }))}
                />
                <Select
                  style={{ width: 110, marginLeft: 8 }}
                  value={batchDatasetMode}
                  onChange={(v) => setBatchDatasetMode(v as 'sequential' | 'random' | 'loop')}
                  options={[
                    { label: t['param.mode.sequential'] || '顺序', value: 'sequential' },
                    { label: t['param.mode.random'] || '随机', value: 'random' },
                    { label: t['param.mode.loop'] || '循环', value: 'loop' },
                  ]}
                />
                {batchDatasetMode === 'loop' && (
                  <InputNumber
                    min={1}
                    max={100}
                    value={batchLoopCount}
                    onChange={(v) => setBatchLoopCount(Number(v) || 1)}
                    style={{ width: 90, marginLeft: 8 }}
                  />
                )}
              </div>
              <div className={styles.initSwitchRow}>
                <Switch
                  checked={batchDatasetFailFast}
                  onChange={(v) => setBatchDatasetFailFast(v)}
                />
                <span className={styles.switchLabel}>{t['param.fail_fast'] || '同一用例首组失败即停'}</span>
              </div>
            </>
          )}
        </Modal>
      </div>
    </>
  );
};

export default TestCases;
