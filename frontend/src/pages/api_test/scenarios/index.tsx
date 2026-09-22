import React, { useCallback, useEffect, useState } from 'react';
import { useHistory } from 'react-router-dom';
import {
  Button,
  Card,
  Divider,
  Drawer,
  Empty,
  Form,
  Input,
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
import { IconDelete, IconDown, IconEdit, IconPlayArrow, IconPlus, IconUp } from '@arco-design/web-react/icon';
import { apiDelete, apiGet, apiPost, apiPut } from '@/utils/apiRequest';
import styles from '../definitions/style/index.module.less';

/** 场景步骤（后端归一化后的形状） */
interface ScenarioStep {
  case_id: number;
  enabled: boolean;
  order: number;
}

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

interface ApiCase {
  id: number;
  name: string;
  api_spec?: { steps?: unknown[] } | null;
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
  const [saving, setSaving] = useState(false);
  const [running, setRunning] = useState(false);

  // ---- 用例选择器 ----
  const [pickerVisible, setPickerVisible] = useState(false);
  const [apiCases, setApiCases] = useState<ApiCase[]>([]);
  const [picked, setPicked] = useState<number[]>([]);

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

  const openDetail = async (id: number) => {
    setDetailLoading(true);
    try {
      const data = await apiGet<ScenarioItem>(`/api/api-test/scenarios/${id}`);
      setSelected(data);
      setEditing(false);
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
  };

  const startEdit = () => {
    if (!selected) return;
    setEditing(true);
    setName(selected.name || '');
    setDescription(selected.description || '');
    setEnvironmentId(selected.environment_id ?? null);
    setVariables((selected.variables || []).map((v) => ({ ...v, enable: v.enable !== false })));
    setSteps((selected.steps || []).map((s) => ({ ...s })));
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
      Message.warning('场景内没有启用的用例');
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
        content: `批次 #${data.batch_id}（${data.total} 条用例），完成后可在执行报告中查看。`,
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
  const openPicker = async () => {
    if (!projectId) return;
    try {
      const data = await apiGet<{ items: ApiCase[] }>(
        `/api/testcases/project/${projectId}/testcases`,
        { case_kind: 'api', size: 200 }
      );
      const items = Array.isArray(data) ? data : (data?.items ?? []);
      const exists = new Set(steps.map((s) => s.case_id));
      setApiCases(items.filter((c) => !exists.has(c.id)));
      setPicked([]);
      setPickerVisible(true);
    } catch {
      Message.error('加载接口用例失败');
    }
  };

  const confirmPick = () => {
    const next = [...steps];
    let order = next.length > 0 ? Math.max(...next.map((s) => s.order || 0)) : 0;
    for (const caseId of picked) {
      order += 1;
      next.push({ case_id: caseId, enabled: true, order });
    }
    setSteps(next);
    setPickerVisible(false);
  };

  const moveStep = (idx: number, dir: -1 | 1) => {
    const j = idx + dir;
    if (j < 0 || j >= steps.length) return;
    const next = [...steps];
    [next[idx], next[j]] = [next[j], next[idx]];
    next.forEach((s, i) => {
      s.order = i + 1;
    });
    setSteps(next);
  };

  const stepCaseName = useCallback(
    (caseId: number): string => {
      const fromDetail = selected?.cases?.find((c) => c.case_id === caseId)?.name;
      if (fromDetail) return fromDetail;
      const fromList = apiCases.find((c) => c.id === caseId)?.name;
      return fromList || `#${caseId}`;
    },
    [selected, apiCases]
  );

  const detailCases = selected?.cases || [];

  return (
    <div className={styles.pageWrap}>
      <div className={styles.pageHeader}>
        <Space>
          <span className={styles.pageTitle}>场景</span>
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
        </Space>
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
                            openDetail(sc.id).then(() => startEdit());
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
                <Form.Item label={`场景用例（${steps.length}）`}>
                  <Table
                    size="small"
                    pagination={false}
                    data={steps.map((s, i) => ({ ...s, _idx: i }))}
                    columns={[
                      { title: '#', dataIndex: 'order', width: 50 },
                      { title: '用例', dataIndex: 'case_id', render: (v: number) => stepCaseName(v) },
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
                  <Button type="text" size="small" icon={<IconPlus />} onClick={openPicker}>
                    添加用例
                  </Button>
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
                      场景用例（{detailCases.length}，启用{' '}
                      {detailCases.filter((c) => c.enabled).length}）
                    </div>
                    {detailCases.length === 0 ? (
                      <Empty description="暂无用例，点击「编辑」添加" />
                    ) : (
                      <Table
                        size="small"
                        pagination={false}
                        data={detailCases.map((c, i) => ({ ...c, _idx: i }))}
                        columns={[
                          { title: '#', dataIndex: 'order', width: 50 },
                          {
                            title: '用例',
                            render: (_: unknown, r: { case_id: number; name?: string | null; missing?: boolean }) =>
                              r.missing ? (
                                <Tag color="red">已删除 #{r.case_id}</Tag>
                              ) : (
                                r.name || `#${r.case_id}`
                              ),
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

      <Drawer
        title="添加接口用例"
        visible={pickerVisible}
        onCancel={() => setPickerVisible(false)}
        onOk={confirmPick}
        okText={`加入 ${picked.length} 条`}
        width={560}
      >
        {apiCases.length === 0 ? (
          <Empty description="没有可添加的接口用例（该项目下暂无，或已全部加入）" />
        ) : (
          <Table
            size="small"
            pagination={{ pageSize: 20 }}
            rowSelection={{
              selectedRowKeys: picked,
              onChange: (keys) => setPicked(keys as number[]),
            }}
            rowKey="id"
            data={apiCases}
            columns={[
              { title: 'ID', dataIndex: 'id', width: 70 },
              { title: '用例名', dataIndex: 'name' },
              {
                title: '步骤',
                width: 80,
                render: (_: unknown, r: ApiCase) => r.api_spec?.steps?.length ?? '—',
              },
            ]}
          />
        )}
      </Drawer>
      <Divider style={{ margin: '8px 0 0' }} />
      <div style={{ color: 'var(--color-text-3)', fontSize: 12 }}>
        场景变量在执行时覆盖用例同名变量（仍低于用例内步骤级变量）；场景执行创建命名批次，报告、暂停/停止与普通批量一致。
      </div>
    </div>
  );
};

export default ApiScenariosPage;
