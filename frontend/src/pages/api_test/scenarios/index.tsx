import React, { useCallback, useEffect, useState } from 'react';
import { useHistory } from 'react-router-dom';
import {
  Button,
  Card,
  Divider,
  Empty,
  Form,
  Input,
  InputNumber,
  List,
  Message,
  Modal,
  Select,
  Space,
  Spin,
  Switch,
  Table,
  Tag,
  Tooltip,
} from '@arco-design/web-react';
import { IconDelete, IconDown, IconDragDotVertical, IconEdit, IconPlayArrow, IconPlus, IconUp } from '@arco-design/web-react/icon';
import { apiDelete, apiGet, apiPost, apiPut } from '@/utils/apiRequest';
import styles from '../definitions/style/index.module.less';
import RequestEditor from '../definitions/components/RequestEditor';
import ImportRequestModal, { PickedCase } from './ImportRequestModal';
import {
  ApiDefinition,
  ApiStep,
  createRequestSpec,
  createStep,
} from '../definitions/types';

/** 场景步骤。容器步骤的子步骤放在 children。 */
interface ScenarioStep {
  id: string;
  type: 'case' | 'definition' | 'request' | 'wait' | 'loop' | 'foreach' | 'if';
  enabled: boolean;
  order: number;
  case_id?: number;
  definition_id?: number;
  name?: string | null;
  request?: ApiStep['request'];
  assertions?: ApiStep['assertions'];
  extractors?: ApiStep['extractors'];
  pre?: ApiStep['pre'];
  post?: ApiStep['post'];
  missing?: boolean;
  definition_name?: string | null;
  method?: string | null;
  path?: string | null;
  ms?: number;
  count?: number;
  dataset_id?: number;
  variable?: string;
  operator?: 'equals' | 'contains';
  expected?: string;
  children?: ScenarioStep[];
}

const newStepId = () => `s${Date.now().toString(36)}${Math.random().toString(36).slice(2, 6)}`;

const asEditableStep = (raw: Partial<ScenarioStep>, index: number): ScenarioStep => {
  const type = raw.type || (raw.case_id != null ? 'case' : 'request');
  return {
    id: raw.id || `legacy-${type}-${raw.case_id ?? raw.definition_id ?? index}`,
    type,
    enabled: raw.enabled !== false,
    order: raw.order || index + 1,
    case_id: raw.case_id,
    definition_id: raw.definition_id,
    name: raw.name,
    request: raw.request,
    assertions: raw.assertions,
    extractors: raw.extractors,
    pre: raw.pre,
    post: raw.post,
    missing: raw.missing,
    definition_name: raw.definition_name,
    method: raw.method,
    path: raw.path,
    ms: raw.ms,
    count: raw.count,
    dataset_id: raw.dataset_id,
    variable: raw.variable,
    operator: raw.operator,
    expected: raw.expected,
    children: (raw.children || []).map((child, i) => asEditableStep(child, i)),
  };
};

const toApiStep = (step: ScenarioStep): ApiStep => {
  const blank = createStep();
  return {
    ...blank,
    name: step.name || blank.name,
    enable: step.enabled,
    definition_id: step.definition_id ?? null,
    request: step.request || createRequestSpec(),
    assertions: step.assertions || [],
    extractors: step.extractors || [],
    pre: step.pre || [],
    post: step.post || [],
  };
};

interface ScenarioVariable {
  key: string;
  value?: string;
  secret?: boolean;
  enable?: boolean;
}

interface ScenarioItem {
  id: number;
  name: string;
  description?: string | null;
  environment_id?: number | null;
  environment_name?: string | null;
  variables?: ScenarioVariable[];
  steps?: ScenarioStep[];
  share_cookie?: boolean;
  continue_on_failure?: boolean;
  step_count?: number;
  enabled_count?: number;
  cases?: Array<{ case_id: number; name?: string | null; enabled: boolean; order?: number; missing?: boolean }>;
  updated_at?: string | null;
}

interface Project {
  id: number;
  name: string;
}

interface Environment {
  id: number;
  name: string;
}

const emptyVar = (): ScenarioVariable => ({ key: '', value: '', secret: false, enable: true });

const ApiScenariosPage: React.FC = () => {
  const history = useHistory();
  const [form] = Form.useForm();
  const [projects, setProjects] = useState<Project[]>([]);
  const [projectId, setProjectId] = useState<number | null>(null);
  const [scenarios, setScenarios] = useState<ScenarioItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [selected, setSelected] = useState<ScenarioItem | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);

  // ---- 编辑态（右侧面板） ----
  const [editing, setEditing] = useState(false);
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [environmentId, setEnvironmentId] = useState<number | null>(null);
  const [environments, setEnvironments] = useState<Environment[]>([]);
  const [variables, setVariables] = useState<ScenarioVariable[]>([]);
  const [steps, setSteps] = useState<ScenarioStep[]>([]);
  const [shareCookie, setShareCookie] = useState(false);
  const [continueOnFailure, setContinueOnFailure] = useState(false);
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [childEditorId, setChildEditorId] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [running, setRunning] = useState(false);

  const [importVisible, setImportVisible] = useState(false);

  const loadProjects = useCallback(async () => {
    try {
      const data = await apiGet<Project[]>('/api/projects/');
      const list = Array.isArray(data) ? data : [];
      setProjects(list);
      if (!projectId && list.length > 0) setProjectId(list[0].id);
    } catch {
      setProjects([]);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const loadScenarios = useCallback(async () => {
    if (!projectId) return;
    setLoading(true);
    try {
      const data = await apiGet<{ total: number; items: ScenarioItem[] }>(
        '/api/api-test/scenarios',
        { project_id: projectId }
      );
      const items = Array.isArray(data) ? data : (data?.items ?? []);
      setScenarios(items);
    } catch {
      setScenarios([]);
    } finally {
      setLoading(false);
    }
  }, [projectId]);

  const loadEnvironments = useCallback(async () => {
    if (!projectId) return;
    try {
      const data = await apiGet<Environment[]>(`/api/projects/${projectId}/environments`);
      setEnvironments(Array.isArray(data) ? data : []);
    } catch {
      setEnvironments([]);
    }
  }, [projectId]);

  useEffect(() => {
    loadProjects();
  }, [loadProjects]);

  useEffect(() => {
    loadScenarios();
    loadEnvironments();
    setSelected(null);
    setEditing(false);
  }, [loadScenarios, loadEnvironments]);

  const fillEditor = (scenario: ScenarioItem) => {
    setSelected(scenario);
    setEditing(true);
    setName(scenario.name || '');
    setDescription(scenario.description || '');
    setEnvironmentId(scenario.environment_id ?? null);
    setVariables((scenario.variables || []).map((v) => ({ ...v, enable: v.enable !== false })));
    setSteps((scenario.steps || []).map((s, i) => asEditableStep(s, i)));
    setShareCookie(!!scenario.share_cookie);
    setContinueOnFailure(!!scenario.continue_on_failure);
    setExpandedId(null);
  };

  const openDetail = async (id: number, edit = false) => {
    setDetailLoading(true);
    try {
      const data = await apiGet<ScenarioItem>(`/api/api-test/scenarios/${id}`);
      if (edit) {
        fillEditor(data);
      } else {
        setSelected(data);
        setEditing(false);
      }
    } catch {
      Message.error('加载场景失败');
    } finally {
      setDetailLoading(false);
    }
  };

  const startCreate = () => {
    setSelected(null);
    setEditing(true);
    setName('');
    setDescription('');
    setEnvironmentId(null);
    setVariables([]);
    setSteps([]);
    setShareCookie(false);
    setContinueOnFailure(false);
    setExpandedId(null);
  };

  const startEdit = () => {
    if (!selected) return;
    fillEditor(selected);
  };

  const handleSave = async () => {
    const trimmed = name.trim();
    if (!trimmed) {
      Message.error('场景名称不能为空');
      return;
    }
    if (!projectId) {
      Message.error('请先选择项目');
      return;
    }
    const payload = {
      project_id: projectId,
      name: trimmed,
      description: description || null,
      environment_id: environmentId,
      variables: variables.filter((v) => v.key.trim()),
      share_cookie: shareCookie,
      continue_on_failure: continueOnFailure,
      steps,
    };
    setSaving(true);
    try {
      let saved: ScenarioItem;
      if (selected) {
        saved = await apiPut<ScenarioItem>(`/api/api-test/scenarios/${selected.id}`, payload, '场景已保存');
      } else {
        saved = await apiPost<ScenarioItem>('/api/api-test/scenarios', payload, '场景已创建');
      }
      await loadScenarios();
      await openDetail(saved.id);
    } catch {
      /* apiRequest 已弹错误 */
    } finally {
      setSaving(false);
    }
  };

  const handleDelete = (sc: ScenarioItem) => {
    Modal.confirm({
      title: '删除场景',
      content: `确定删除场景「${sc.name}」？其引用的用例与历史批次不受影响。`,
      onOk: async () => {
        try {
          await apiDelete(`/api/api-test/scenarios/${sc.id}`, '场景已删除');
          if (selected?.id === sc.id) {
            setSelected(null);
            setEditing(false);
          }
          await loadScenarios();
        } catch {
          /* 已弹错误 */
        }
      },
    });
  };

  const handleRun = async () => {
    if (!selected) return;
    if (!selected.environment_id) {
      Message.warning('该场景未绑定环境，请先编辑场景选择环境');
      return;
    }
    if ((selected.enabled_count ?? selected.steps?.length ?? 0) === 0) {
      Message.warning('场景内没有启用的步骤');
      return;
    }
    setRunning(true);
    try {
      const data = await apiPost<{ batch_id: number; total: number }>(
        `/api/api-test/scenarios/${selected.id}/run`,
        {},
        '场景执行已启动'
      );
      Modal.success({
        title: '场景执行已启动',
        content: `批次 #${data.batch_id}（${data.total} 个步骤），完成后可在执行报告中查看每步请求与断言。`,
        okText: '查看报告',
        onOk: () => history.push('/reports'),
      });
    } catch {
      /* 已弹错误 */
    } finally {
      setRunning(false);
    }
  };

  // ---- 变量表 ----
  const addVariable = () => setVariables([...variables, emptyVar()]);
  const updateVariable = (idx: number, patch: Partial<ScenarioVariable>) =>
    setVariables(variables.map((v, i) => (i === idx ? { ...v, ...patch } : v)));
  const removeVariable = (idx: number) => setVariables(variables.filter((_, i) => i !== idx));

  // ---- 步骤 ----
  const confirmImport = (defs: ApiDefinition[], pickedCases: PickedCase[]) => {
    const next = [...steps];
    let order = next.length;
    for (const def of defs) {
      const request = createRequestSpec();
      request.method = def.method || 'GET';
      request.url = `{{baseUrl}}${def.path || '/'}`;
      order += 1;
      next.push({
        id: newStepId(),
        type: 'definition',
        definition_id: def.id,
        name: def.name,
        definition_name: def.name,
        method: def.method,
        path: def.path,
        enabled: true,
        order,
        request,
        assertions: [],
        extractors: [],
        pre: [],
      });
    }
    for (const item of pickedCases) {
      order += 1;
      next.push({
        id: newStepId(),
        type: 'case',
        case_id: item.id,
        name: item.name,
        method: item.method,
        path: item.path,
        definition_name: item.definitionName,
        enabled: true,
        order,
      });
    }
    setSteps(next);
    setImportVisible(false);
  };

  const addCustomRequest = () => {
    const request = createRequestSpec();
    setSteps([
      ...steps,
      {
        id: newStepId(),
        type: 'request',
        name: '自定义请求',
        enabled: true,
        order: steps.length + 1,
        request,
        assertions: [],
        extractors: [],
        pre: [],
      },
    ]);
  };

  const mapSteps = (
    items: ScenarioStep[],
    id: string,
    patch: Partial<ScenarioStep>
  ): ScenarioStep[] =>
    items.map((s) => {
      if (s.id === id) return { ...s, ...patch };
      if (s.children?.length) return { ...s, children: mapSteps(s.children, id, patch) };
      return s;
    });

  const findStep = (items: ScenarioStep[], id: string): ScenarioStep | undefined => {
    for (const item of items) {
      if (item.id === id) return item;
      const nested = item.children ? findStep(item.children, id) : undefined;
      if (nested) return nested;
    }
    return undefined;
  };

  const patchStep = (id: string, patch: Partial<ScenarioStep>) =>
    setSteps(mapSteps(steps, id, patch));

  const applyApiStep = (id: string, apiStep: ApiStep) =>
    patchStep(id, {
      name: apiStep.name,
      request: apiStep.request,
      assertions: apiStep.assertions,
      extractors: apiStep.extractors,
      pre: apiStep.pre,
      post: apiStep.post,
    });

  const stepLabel = (step: ScenarioStep): string => {
    if (step.type === 'case') {
      if (step.missing) return `已删除 #${step.case_id}`;
      const caseName = step.name || stepCaseName(step.case_id || 0);
      const api = [step.method, step.path].filter(Boolean).join(' ');
      return api ? `${caseName} · ${api}` : caseName;
    }
    if (step.type === 'definition') {
      return step.name || step.definition_name || `接口 #${step.definition_id}`;
    }
    if (step.type === 'wait') return step.name || `等待 ${step.ms || 0} ms`;
    if (step.type === 'loop') return step.name || `循环 ${step.count || 1} 次`;
    if (step.type === 'foreach') return step.name || `遍历数据集 #${step.dataset_id || ''}`;
    if (step.type === 'if') return step.name || `若 ${step.variable || ''} ${step.operator === 'contains' ? '包含' : '等于'} ${step.expected || ''}`;
    return step.name || step.request?.url || '自定义请求';
  };

  const typeLabel = (type: ScenarioStep['type']) => {
    if (type === 'case') return '用例';
    if (type === 'definition') return '接口';
    if (type === 'wait') return '等待';
    if (type === 'loop') return '循环';
    if (type === 'foreach') return '遍历';
    if (type === 'if') return '条件';
    return '自定义';
  };

  const addController = (type: ScenarioStep['type']) => {
    const base: ScenarioStep = {
      id: newStepId(),
      type,
      name: typeLabel(type),
      enabled: true,
      order: steps.length + 1,
      children: [],
    };
    if (type === 'wait') base.ms = 1000;
    if (type === 'loop') base.count = 2;
    if (type === 'if') {
      base.variable = '';
      base.operator = 'equals';
      base.expected = '';
    }
    setSteps([...steps, base]);
  };

  const addChildRequest = (parentId: string) => {
    const parent = findStep(steps, parentId);
    const request = createRequestSpec();
    const child: ScenarioStep = {
      id: newStepId(),
      type: 'request',
      name: '子请求',
      enabled: true,
      order: (parent?.children?.length || 0) + 1,
      request,
      assertions: [],
      extractors: [],
      pre: [],
    };
    patchStep(parentId, { children: [...(parent?.children || []), child] });
  };

  const renumber = (list: ScenarioStep[]) => list.map((s, i) => ({ ...s, order: i + 1 }));

  const moveStep = (idx: number, dir: -1 | 1) => {
    const j = idx + dir;
    if (j < 0 || j >= steps.length) return;
    const next = [...steps];
    [next[idx], next[j]] = [next[j], next[idx]];
    setSteps(renumber(next));
  };

  const reorderSteps = (fromId: string, toId: string) => {
    if (!fromId || fromId === toId) return;
    const from = steps.findIndex((s) => s.id === fromId);
    const to = steps.findIndex((s) => s.id === toId);
    if (from < 0 || to < 0) return;
    const next = [...steps];
    const [item] = next.splice(from, 1);
    next.splice(to, 0, item);
    setSteps(renumber(next));
  };

  const reorderChildren = (parentId: string, fromId: string, toId: string) => {
    if (!fromId || fromId === toId) return;
    const parent = findStep(steps, parentId);
    const list = [...(parent?.children || [])];
    const from = list.findIndex((s) => s.id === fromId);
    const to = list.findIndex((s) => s.id === toId);
    if (from < 0 || to < 0) return;
    const [item] = list.splice(from, 1);
    list.splice(to, 0, item);
    patchStep(parentId, { children: renumber(list) });
  };

  const stepCaseName = useCallback(
    (caseId: number): string => {
      const fromDetail = selected?.cases?.find((c) => c.case_id === caseId)?.name;
      if (fromDetail) return fromDetail;
      return `#${caseId}`;
    },
    [selected]
  );

  return (
    <div className={styles.pageWrap}>
      <div className={styles.pageHeader}>
        <div>
          <div className={styles.pageTitle}>场景</div>
          <div className={styles.pageHint}>把接口编排成可重复执行的流程。</div>
          <Select
            placeholder="选择项目"
            value={projectId ?? undefined}
            onChange={(v) => setProjectId(v as number)}
            style={{ width: 220 }}
          >
            {projects.map((p) => (
              <Select.Option key={p.id} value={p.id}>
                {p.name}
              </Select.Option>
            ))}
          </Select>
        </div>
        <Space>
          <Button type="primary" icon={<IconPlus />} onClick={startCreate}>
            新建场景
          </Button>
        </Space>
      </div>

      <div className={styles.splitLayout}>
        <Card className={styles.leftPane} title={`场景列表（${scenarios.length}）`} bordered={false}>
          <Spin loading={loading} style={{ width: '100%' }}>
            {scenarios.length === 0 && !loading ? (
              <Empty description="暂无场景，点击右上「新建场景」" />
            ) : (
              <List
                dataSource={scenarios}
                render={(sc) => (
                  <List.Item
                    key={sc.id}
                    className={selected?.id === sc.id ? styles.selectedRow : undefined}
                    onClick={() => openDetail(sc.id)}
                    style={{ cursor: 'pointer' }}
                    actions={[
                      <Tooltip content="编辑" key="edit">
                        <Button
                          type="text"
                          size="mini"
                          icon={<IconEdit />}
                          aria-label="编辑场景"
                          onClick={(e) => {
                            e.stopPropagation();
                            openDetail(sc.id, true);
                          }}
                        />
                      </Tooltip>,
                      <Tooltip content="删除" key="del">
                        <Button
                          type="text"
                          size="mini"
                          status="danger"
                          icon={<IconDelete />}
                          aria-label="删除场景"
                          onClick={(e) => {
                            e.stopPropagation();
                            handleDelete(sc);
                          }}
                        />
                      </Tooltip>,
                    ]}
                  >
                    <List.Item.Meta
                      title={sc.name}
                      description={
                        <Space wrap>
                          <span>{sc.environment_name || '未绑定环境'}</span>
                          <Tag color="arcoblue">
                            {sc.enabled_count ?? 0}/{sc.step_count ?? 0} 用例
                          </Tag>
                        </Space>
                      }
                    />
                  </List.Item>
                )}
              />
            )}
          </Spin>
        </Card>

        <Card
          className={styles.rightPane}
          title={editing ? (selected ? '编辑场景' : '新建场景') : '场景明细'}
          bordered={false}
          extra={
            !editing && selected ? (
              <Space>
                <Button icon={<IconEdit />} onClick={startEdit}>
                  编辑
                </Button>
                <Button
                  type="primary"
                  icon={<IconPlayArrow />}
                  loading={running}
                  onClick={handleRun}
                >
                  执行场景
                </Button>
              </Space>
            ) : null
          }
        >
          <Spin loading={detailLoading} style={{ width: '100%' }}>
            {!selected && !editing ? (
              <Empty description="从左侧选择一个场景，或新建场景" />
            ) : editing ? (
              <Form layout="vertical">
                <Form.Item label="场景名称" required>
                  <Input value={name} onChange={setName} placeholder="如 登录→下单全链路" maxLength={200} />
                </Form.Item>
                <Form.Item label="描述">
                  <Input.TextArea
                    value={description}
                    onChange={setDescription}
                    placeholder="场景说明（可选）"
                    autoSize={{ minRows: 2 }}
                  />
                </Form.Item>
                <Form.Item label="执行环境" required>
                  <Select
                    placeholder="选择执行环境（决定 baseUrl）"
                    value={environmentId ?? undefined}
                    onChange={(v) => setEnvironmentId(v as number)}
                    allowClear
                  >
                    {environments.map((e) => (
                      <Select.Option key={e.id} value={e.id}>
                        {e.name}
                      </Select.Option>
                    ))}
                  </Select>
                </Form.Item>
                <Form.Item label="场景变量（执行时覆盖用例同名变量）">
                  <Table
                    size="small"
                    pagination={false}
                    data={variables.map((v, i) => ({ ...v, _idx: i }))}
                    columns={[
                      { title: '变量名', dataIndex: 'key', render: (_: unknown, r: { _idx: number }) => (
                          <Input
                            value={variables[r._idx].key}
                            onChange={(v) => updateVariable(r._idx, { key: v })}
                            placeholder="如 token"
                          />
                        ) },
                      { title: '值', dataIndex: 'value', render: (_: unknown, r: { _idx: number }) => (
                          <Input
                            value={String(variables[r._idx].value ?? '')}
                            onChange={(v) => updateVariable(r._idx, { value: v })}
                            placeholder="具体值"
                          />
                        ) },
                      { title: '加密', dataIndex: 'secret', width: 70, render: (_: unknown, r: { _idx: number }) => (
                          <Switch
                            checked={!!variables[r._idx].secret}
                            onChange={(v) => updateVariable(r._idx, { secret: v })}
                          />
                        ) },
                      { title: '启用', dataIndex: 'enable', width: 70, render: (_: unknown, r: { _idx: number }) => (
                          <Switch
                            checked={variables[r._idx].enable !== false}
                            onChange={(v) => updateVariable(r._idx, { enable: v })}
                          />
                        ) },
                      { title: '操作', width: 70, render: (_: unknown, r: { _idx: number }) => (
                          <Button
                            type="text"
                            size="mini"
                            status="danger"
                            icon={<IconDelete />}
                            onClick={() => removeVariable(r._idx)}
                          />
                        ) },
                    ]}
                  />
                  <Button type="text" size="small" icon={<IconPlus />} onClick={addVariable}>
                    添加变量
                  </Button>
                </Form.Item>
                <Form.Item label="执行选项">
                  <Space size="large">
                    <span>
                      共享 Cookie{' '}
                      <Switch checked={shareCookie} onChange={setShareCookie} />
                    </span>
                    <span>
                      失败继续{' '}
                      <Switch checked={continueOnFailure} onChange={setContinueOnFailure} />
                    </span>
                  </Space>
                </Form.Item>
                <Form.Item label={`场景步骤（${steps.length}）`}>
                  <Table
                    size="small"
                    pagination={false}
                    data={steps.map((s, i) => ({ ...s, _idx: i }))}
                    expandedRowKeys={expandedId ? [expandedId] : []}
                    onExpandedRowsChange={(keys) =>
                      setExpandedId(keys.length ? String(keys[keys.length - 1]) : null)
                    }
                    rowKey="id"
                    onRow={(record) => ({
                      onDragOver: (event) => event.preventDefault(),
                      onDrop: (event) => {
                        event.preventDefault();
                        reorderSteps(event.dataTransfer.getData('text/plain'), record.id);
                      },
                    })}
                    expandedRowRender={(row: ScenarioStep) => {
                      if (row.type === 'case') {
                        return <span>引用用例，执行时使用该用例自己的请求。变量仍会被场景变量覆盖。</span>;
                      }
                      if (row.type === 'wait') {
                        return (
                          <span>
                            等待毫秒（最多 60000）{' '}
                            <InputNumber min={0} max={60000} value={row.ms || 0} onChange={(v) => patchStep(row.id, { ms: v || 0 })} />
                          </span>
                        );
                      }
                      if (row.type === 'loop' || row.type === 'foreach' || row.type === 'if') {
                        return (
                          <div>
                            {row.type === 'loop' ? (
                              <div>次数 <InputNumber min={1} max={100} value={row.count || 1} onChange={(v) => patchStep(row.id, { count: v || 1 })} /></div>
                            ) : null}
                            {row.type === 'foreach' ? (
                              <div>数据集 ID <InputNumber min={1} value={row.dataset_id} onChange={(v) => patchStep(row.id, { dataset_id: v || undefined })} /></div>
                            ) : null}
                            {row.type === 'if' ? (
                              <Space>
                                <Input style={{ width: 120 }} placeholder="变量名" value={row.variable} onChange={(v) => patchStep(row.id, { variable: v })} />
                                <Select
                                  style={{ width: 100 }}
                                  value={row.operator || 'equals'}
                                  onChange={(v) => patchStep(row.id, { operator: v as 'equals' | 'contains' })}
                                  options={[{ label: '等于', value: 'equals' }, { label: '包含', value: 'contains' }]}
                                />
                                <Input style={{ width: 160 }} placeholder="期望值" value={row.expected} onChange={(v) => patchStep(row.id, { expected: v })} />
                              </Space>
                            ) : null}
                            <div style={{ marginTop: 8 }}>
                              {(row.children || []).map((child) => (
                                <div key={child.id}>
                                  <div
                                    className={styles.childStep}
                                    onDragOver={(event) => event.preventDefault()}
                                    onDrop={(event) => {
                                      event.preventDefault();
                                      event.stopPropagation();
                                      reorderChildren(row.id, event.dataTransfer.getData('text/plain'), child.id);
                                    }}
                                  >
                                    <span
                                      draggable
                                      aria-label="拖动排序"
                                      className={styles.dragHandle}
                                      onDragStart={(event) => {
                                        event.dataTransfer.effectAllowed = 'move';
                                        event.dataTransfer.setData('text/plain', child.id);
                                        event.stopPropagation();
                                      }}
                                    >
                                      <IconDragDotVertical />
                                    </span>
                                    {typeLabel(child.type)} · {stepLabel(child)}
                                    {child.type === 'request' || child.type === 'definition' ? (
                                      <Button
                                        type="text"
                                        size="mini"
                                        onClick={() => setChildEditorId(childEditorId === child.id ? null : child.id)}
                                      >
                                        {childEditorId === child.id ? '收起' : '编辑请求'}
                                      </Button>
                                    ) : null}
                                    <Button type="text" size="mini" status="danger" onClick={() => patchStep(row.id, { children: (row.children || []).filter((c) => c.id !== child.id) })}>删除</Button>
                                  </div>
                                  {childEditorId === child.id && (child.type === 'request' || child.type === 'definition') ? (
                                    <RequestEditor
                                      embedded
                                      spec={toApiStep(child)}
                                      definitionName={child.definition_name || child.name}
                                      onChange={(next) => applyApiStep(child.id, next)}
                                    />
                                  ) : null}
                                </div>
                              ))}
                              <Button type="text" size="mini" onClick={() => addChildRequest(row.id)}>添加子请求</Button>
                            </div>
                          </div>
                        );
                      }
                      return (
                        <RequestEditor
                          embedded
                          spec={toApiStep(row)}
                          definitionName={row.definition_name || row.name}
                          onChange={(next) => applyApiStep(row.id, next)}
                        />
                      );
                    }}
                    columns={[
                      {
                        title: '',
                        width: 36,
                        render: (_: unknown, r: ScenarioStep) => (
                          <span
                            draggable
                            aria-label="拖动排序"
                            className={styles.dragHandle}
                            onDragStart={(event) => {
                              event.dataTransfer.effectAllowed = 'move';
                              event.dataTransfer.setData('text/plain', r.id);
                              event.stopPropagation();
                            }}
                          >
                            <IconDragDotVertical />
                          </span>
                        ),
                      },
                      { title: '#', dataIndex: 'order', width: 50 },
                      {
                        title: '类型',
                        width: 90,
                        render: (_: unknown, r: ScenarioStep) => typeLabel(r.type),
                      },
                      { title: '步骤', render: (_: unknown, r: ScenarioStep) => stepLabel(r) },
                      { title: '启用', dataIndex: 'enabled', width: 70, render: (_: unknown, r: { _idx: number }) => (
                          <Switch
                            checked={!!steps[r._idx].enabled}
                            onChange={(v) =>
                              setSteps(steps.map((s, i) => (i === r._idx ? { ...s, enabled: v } : s)))
                            }
                          />
                        ) },
                      { title: '操作', width: 130, render: (_: unknown, r: { _idx: number }) => (
                          <Space>
                            <Button
                              type="text" size="mini" icon={<IconUp />}
                              disabled={r._idx === 0} onClick={() => moveStep(r._idx, -1)}
                            />
                            <Button
                              type="text" size="mini" icon={<IconDown />}
                              disabled={r._idx === steps.length - 1} onClick={() => moveStep(r._idx, 1)}
                            />
                            <Button
                              type="text" size="mini" status="danger" icon={<IconDelete />}
                              onClick={() => setSteps(steps.filter((_, i) => i !== r._idx))}
                            />
                          </Space>
                        ) },
                    ]}
                  />
                  <Space>
                    <Button type="text" size="small" icon={<IconPlus />} onClick={() => setImportVisible(true)}>
                      导入
                    </Button>
                    <Button type="text" size="small" icon={<IconPlus />} onClick={addCustomRequest}>
                      自定义请求
                    </Button>
                    <Button type="text" size="small" onClick={() => addController('wait')}>等待</Button>
                    <Button type="text" size="small" onClick={() => addController('loop')}>循环</Button>
                    <Button type="text" size="small" onClick={() => addController('foreach')}>遍历</Button>
                    <Button type="text" size="small" onClick={() => addController('if')}>条件</Button>
                  </Space>
                </Form.Item>
                <Space>
                  <Button type="primary" loading={saving} onClick={handleSave}>
                    保存
                  </Button>
                  <Button
                    onClick={() => {
                      if (selected) {
                        setEditing(false);
                        openDetail(selected.id);
                      } else {
                        setEditing(false);
                      }
                    }}
                  >
                    取消
                  </Button>
                </Space>
              </Form>
            ) : selected ? (
              <div>
                <Space direction="vertical" style={{ width: '100%' }} size="medium">
                  <div>
                    <div className={styles.detailLabel}>描述</div>
                    <div>{selected.description || '—'}</div>
                  </div>
                  <div>
                    <div className={styles.detailLabel}>执行环境</div>
                    <div>{selected.environment_name || '未绑定'}</div>
                  </div>
                  <div>
                    <Space>
                      {selected.share_cookie ? <Tag color="arcoblue">共享 Cookie</Tag> : <Tag>不共享 Cookie</Tag>}
                      {selected.continue_on_failure ? <Tag color="orange">失败继续</Tag> : <Tag>失败即停</Tag>}
                    </Space>
                  </div>
                  <div>
                    <div className={styles.detailLabel}>
                      场景变量（{selected.variables?.length ?? 0}）
                    </div>
                    {(selected.variables || []).length === 0 ? (
                      <div>—</div>
                    ) : (
                      <Table
                        size="small"
                        pagination={false}
                        data={(selected.variables || []).map((v, i) => ({ ...v, _idx: i }))}
                        columns={[
                          { title: '变量名', dataIndex: 'key' },
                          {
                            title: '值',
                            dataIndex: 'value',
                            render: (v: unknown, r: ScenarioVariable) =>
                              r.secret ? '******' : String(v ?? ''),
                          },
                          {
                            title: '状态',
                            render: (_: unknown, r: ScenarioVariable) => (
                              <Space>
                                {r.secret ? <Tag color="orange">加密</Tag> : null}
                                {r.enable === false ? <Tag>停用</Tag> : <Tag color="green">启用</Tag>}
                              </Space>
                            ),
                          },
                        ]}
                      />
                    )}
                  </div>
                  <div>
                    <div className={styles.detailLabel}>
                      场景步骤（{(selected.steps || []).length}，启用{' '}
                      {(selected.steps || []).filter((c) => c.enabled).length}）
                    </div>
                    {(selected.steps || []).length === 0 ? (
                      <Empty description="暂无步骤，点击「编辑」添加" />
                    ) : (
                      <Table
                        size="small"
                        pagination={false}
                        rowKey="id"
                        data={(selected.steps || []).map((c, i) => asEditableStep(c, i))}
                        columns={[
                          { title: '#', dataIndex: 'order', width: 50 },
                          {
                            title: '类型',
                            width: 90,
                            render: (_: unknown, r: ScenarioStep) =>
                              typeLabel(r.type),
                          },
                          {
                            title: '步骤',
                            render: (_: unknown, r: ScenarioStep) =>
                              r.missing ? <Tag color="red">{stepLabel(r)}</Tag> : stepLabel(r),
                          },
                          {
                            title: '启用',
                            width: 80,
                            render: (_: unknown, r: { enabled: boolean }) =>
                              r.enabled ? <Tag color="green">启用</Tag> : <Tag>停用</Tag>,
                          },
                        ]}
                      />
                    )}
                  </div>
                </Space>
              </div>
            ) : null}
          </Spin>
        </Card>
      </div>

      <ImportRequestModal
        visible={importVisible}
        projectId={projectId}
        existingCaseIds={steps.filter((s) => s.type === 'case' && s.case_id).map((s) => s.case_id as number)}
        onCancel={() => setImportVisible(false)}
        onConfirm={confirmImport}
      />
      <Divider style={{ margin: '8px 0 0' }} />
      <div style={{ color: 'var(--color-text-3)', fontSize: 12 }}>
        场景变量在执行时覆盖用例同名变量（仍低于用例内步骤级变量）；场景执行创建命名批次，报告、暂停/停止与普通批量一致。
      </div>
    </div>
  );
};

export default ApiScenariosPage;
