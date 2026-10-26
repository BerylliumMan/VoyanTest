import React, { useEffect, useMemo, useState } from 'react';
import { useHistory, useParams } from 'react-router-dom';
import axios from 'axios';
import {
  Button,
  Card,
  Drawer,
  Input,
  Message,
  Modal,
  Select,
  Space,
  Spin,
  Table,
  Tag,
  Typography,
} from '@arco-design/web-react';
import {
  IconArrowLeft,
  IconDelete,
  IconPlus,
  IconSave,
  IconPlayArrow,
  IconCopy,
  IconUp,
  IconDown,
  IconHistory,
} from '@arco-design/web-react/icon';
import RequestEditor from '../../definitions/components/RequestEditor';
import ResponsePanel from '../../definitions/components/ResponsePanel';
import {
  ApiDefinition,
  ApiStep,
  DebugResponse,
  createRequestSpec,
  createStep,
} from '../../definitions/types';

const { Text, Title } = Typography;

interface Project { id: number; name: string; }
interface Module { id: number; name: string; parent_id: number | null; }
interface Environment { id: number; name: string; is_default?: boolean; }

const emptySpec = () => ({
  schema_version: 1,
  variables: [],
  dataset_id: null,
  dataset_mode: 'sequential',
  loop_count: 1,
  fail_policy: 'fail_fast',
  steps: [createStep()] as ApiStep[],
});

const normalizeStep = (raw: any, index: number): ApiStep => ({
  ...createStep(),
  ...(raw || {}),
  order: index + 1,
  request: { ...createRequestSpec(), ...((raw || {}).request || {}) },
  assertions: Array.isArray(raw?.assertions) ? raw.assertions : [],
  extractors: Array.isArray(raw?.extractors) ? raw.extractors : [],
  pre: Array.isArray(raw?.pre) ? raw.pre : [],
  post: Array.isArray(raw?.post) ? raw.post : [],
});

const stepFromDefinition = (definition: ApiDefinition): ApiStep => {
  const step = createStep();
  step.name = definition.name || `${definition.method} ${definition.path}`;
  step.definition_id = definition.id;
  const request = createRequestSpec();
  request.method = definition.method || 'GET';
  request.url = `{{baseUrl}}${definition.path || '/'}`;
  const schema: any = (definition as any).request_schema || {};
  const query = (schema.params || [])
    .filter((p: any) => String(p?.in || '') === 'query')
    .map((p: any) => ({
      key: String(p.name || ''),
      value: p.example == null ? '' : String(p.example),
      enable: true,
      secret: false,
    }));
  request.query = query;
  const body = schema.body || {};
  const contentType = String(body.content_type || '');
  if (contentType) {
    request.headers = [{ key: 'Content-Type', value: contentType, enable: true, secret: false }];
  }
  if (contentType.toLowerCase().includes('json')) {
    request.body = {
      type: 'json',
      content: body.example == null ? '' : (typeof body.example === 'string' ? body.example : JSON.stringify(body.example, null, 2)),
      form_data: [],
      files: [],
    };
  } else if (contentType && body.example != null) {
    request.body = {
      type: 'raw',
      content: typeof body.example === 'string' ? body.example : JSON.stringify(body.example, null, 2),
      form_data: [],
      files: [],
    };
  }
  step.request = request;
  step.assertions = [{
    enable: true,
    type: 'status_code',
    condition: 'equals',
    expected: '200',
    name: '状态码 200',
  }] as any;
  return step;
};

const ApiCaseEditorPage: React.FC = () => {
  const history = useHistory();
  const params = useParams<{ id: string }>();
  const isNew = params.id === 'new';
  const query = useMemo(() => new URLSearchParams(window.location.search), []);
  const projectId = Number(query.get('project_id') || 0) || null;
  const definitionIds = useMemo(() => {
    const raw = query.get('definitions') || '';
    return raw.split(',').map((x) => Number(x)).filter((x) => Number.isFinite(x) && x > 0);
  }, [query]);

  const [loading, setLoading] = useState(!isNew);
  const [saving, setSaving] = useState(false);
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [moduleId, setModuleId] = useState<number | null>(null);
  const [priority, setPriority] = useState('medium');
  const [tags, setTags] = useState('');
  const [steps, setSteps] = useState<ApiStep[]>([createStep()]);
  const [activeIndex, setActiveIndex] = useState(0);
  const [modules, setModules] = useState<Module[]>([]);
  const [environments, setEnvironments] = useState<Environment[]>([]);
  const [environmentId, setEnvironmentId] = useState<number | null>(null);
  const [definitions, setDefinitions] = useState<ApiDefinition[]>([]);
  const [pickDefinitionId, setPickDefinitionId] = useState<number | null>(null);
  const [response, setResponse] = useState<DebugResponse | null>(null);
  const [sending, setSending] = useState(false);
  const [revisionsVisible, setRevisionsVisible] = useState(false);
  const [revisions, setRevisions] = useState<any[]>([]);
  const [revisionsLoading, setRevisionsLoading] = useState(false);

  useEffect(() => {
    if (!projectId) return;
    axios.get(`/api/projects/${projectId}/modules`).then((res) => setModules(res.data || [])).catch(() => setModules([]));
    axios.get(`/api/projects/${projectId}/environments`).then((res) => {
      const list = res.data || [];
      setEnvironments(list);
      const def = list.find((e: Environment) => e.is_default);
      setEnvironmentId(def ? def.id : (list[0]?.id || null));
    }).catch(() => setEnvironments([]));
    axios.get('/api/api-test/definitions', { params: { project_id: projectId, page_size: 500 } })
      .then((res) => setDefinitions(res.data?.items || []))
      .catch(() => setDefinitions([]));
  }, [projectId]);

  useEffect(() => {
    if (isNew) {
      if (definitionIds.length > 0) {
        axios.get('/api/api-test/definitions', { params: { project_id: projectId, page_size: 500 } })
          .then((res) => {
            const all: ApiDefinition[] = res.data?.items || [];
            const picked = definitionIds
              .map((id) => all.find((d) => d.id === id))
              .filter(Boolean) as ApiDefinition[];
            if (picked.length) {
              setName(picked.length === 1 ? picked[0].name : `接口用例（${picked.length} 个接口）`);
              setSteps(picked.map(stepFromDefinition));
              setActiveIndex(0);
            }
          })
          .catch(() => undefined);
      }
      setLoading(false);
      return;
    }
    const id = Number(params.id);
    setLoading(true);
    axios.get(`/api/testcases/${id}`)
      .then((res) => {
        const data = res.data || {};
        setName(data.name || '');
        setDescription(data.description || '');
        setModuleId(data.module_id ?? null);
        setPriority(data.priority || 'medium');
        setTags(data.tags || '');
        const spec = data.api_spec || {};
        const rawSteps = Array.isArray(spec.steps) && spec.steps.length ? spec.steps : [createStep()];
        setSteps(rawSteps.map(normalizeStep));
        setActiveIndex(0);
      })
      .catch(() => Message.error('加载用例失败'))
      .finally(() => setLoading(false));
  }, [isNew, params.id, projectId, definitionIds]);

  const current = steps[activeIndex] || steps[0] || createStep();

  const updateCurrent = (next: ApiStep) => {
    setSteps((prev) => prev.map((step, i) => (i === activeIndex ? { ...next, order: i + 1 } : step)));
  };

  const addStep = () => {
    setSteps((prev) => [...prev, { ...createStep(), order: prev.length + 1 }]);
    setActiveIndex(steps.length);
  };

  const duplicateStep = () => {
    const copy = JSON.parse(JSON.stringify(current));
    copy.name = `${copy.name || '请求'}（副本）`;
    setSteps((prev) => {
      const next = [...prev];
      next.splice(activeIndex + 1, 0, copy);
      return next.map((step, i) => ({ ...step, order: i + 1 }));
    });
    setActiveIndex(activeIndex + 1);
  };

  const deleteStep = () => {
    if (steps.length <= 1) {
      Message.warning('至少保留一个步骤');
      return;
    }
    setSteps((prev) => prev.filter((_, i) => i !== activeIndex).map((step, i) => ({ ...step, order: i + 1 })));
    setActiveIndex(Math.max(0, activeIndex - 1));
  };

  const moveStep = (offset: number) => {
    const target = activeIndex + offset;
    if (target < 0 || target >= steps.length) return;
    setSteps((prev) => {
      const next = [...prev];
      [next[activeIndex], next[target]] = [next[target], next[activeIndex]];
      return next.map((step, i) => ({ ...step, order: i + 1 }));
    });
    setActiveIndex(target);
  };

  const fillFromDefinition = () => {
    const definition = definitions.find((d) => d.id === pickDefinitionId);
    if (!definition) {
      Message.warning('请选择接口定义');
      return;
    }
    updateCurrent({ ...stepFromDefinition(definition), order: current.order });
  };

  const handleSend = async () => {
    if (!environmentId) {
      Message.warning('请先选择执行环境');
      return;
    }
    setSending(true);
    try {
      const res = await axios.post('/api/api-test/debug', {
        environment_id: environmentId,
        case_variables: [],
        step: current,
        api_spec: { schema_version: 1, variables: [], steps: [current] },
        dry_run: false,
      });
      setResponse(res.data);
    } catch (err: any) {
      Message.error(err?.response?.data?.detail || '发送失败');
    } finally {
      setSending(false);
    }
  };

  const saveCase = async (andRun = false): Promise<number | null> => {
    if (!name.trim()) {
      Message.warning('请填写用例名称');
      return null;
    }
    setSaving(true);
    const spec = {
      schema_version: 1,
      variables: [],
      dataset_id: null,
      dataset_mode: 'sequential',
      loop_count: 1,
      fail_policy: 'fail_fast',
      steps: steps.map((step, i) => ({ ...step, order: i + 1 })),
    };
    const payload: any = {
      name: name.trim(),
      description: description || '',
      module_id: moduleId,
      priority,
      tags: tags || null,
      steps: [],
      api_spec: spec,
    };
    try {
      let caseId = Number(params.id);
      if (isNew) {
        const res = await axios.post('/api/testcases/', {
          ...payload,
          project_id: projectId,
          case_kind: 'api',
        });
        caseId = res.data?.id;
        Message.success('用例已创建');
      } else {
        await axios.put(`/api/testcases/${caseId}`, payload);
        Message.success('用例已保存');
      }
      if (andRun && caseId) {
        if (!environmentId) {
          Message.warning('已保存，但未选择执行环境');
        } else {
          const res = await axios.post('/api/testcases/batch-run', {
            case_ids: [caseId],
            environment_id: environmentId,
          });
          const batchId = res.data?.batch_id || res.data?.id;
          Message.success(`已提交执行${batchId ? `（批次 #${batchId}）` : ''}`);
        }
      }
      if (!andRun) history.push(`/api_test/cases?project_id=${projectId}`);
      return caseId;
    } catch (err: any) {
      const detail = err?.response?.data?.detail;
      if (detail && typeof detail === 'object' && Array.isArray(detail.problems)) {
        Message.error(`保存失败：${detail.problems.slice(0, 3).join('；')}`);
      } else {
        Message.error(typeof detail === 'string' ? detail : '保存失败');
      }
      return null;
    } finally {
      setSaving(false);
    }
  };

  const loadRevisions = async () => {
    if (isNew) {
      Message.info('新建用例还没有历史版本');
      return;
    }
    setRevisionsVisible(true);
    setRevisionsLoading(true);
    try {
      const res = await axios.get(`/api/testcases/${params.id}/revisions`);
      setRevisions(res.data?.items || []);
    } catch {
      setRevisions([]);
    } finally {
      setRevisionsLoading(false);
    }
  };

  const restoreRevision = async (revisionId: number) => {
    try {
      await axios.post(`/api/testcases/${params.id}/revisions/${revisionId}/restore`);
      Message.success('已恢复到该版本');
      setRevisionsVisible(false);
      window.location.reload();
    } catch (err: any) {
      Message.error(err?.response?.data?.detail || '恢复失败');
    }
  };

  if (loading) {
    return <div style={{ padding: 48, textAlign: 'center' }}><Spin /></div>;
  }

  return (
    <div style={{ padding: 16 }}>
      <Card>
        <Space style={{ marginBottom: 12 }} wrap>
          <Button icon={<IconArrowLeft />} onClick={() => history.push(`/api_test/cases?project_id=${projectId}`)}>
            返回接口用例
          </Button>
          <Button type="primary" icon={<IconSave />} loading={saving} onClick={() => saveCase(false)}>保存</Button>
          <Button icon={<IconPlayArrow />} loading={saving} onClick={() => saveCase(true)}>保存并执行</Button>
          <Button icon={<IconHistory />} onClick={loadRevisions}>变更记录</Button>
          <Select
            style={{ width: 200 }}
            value={environmentId ?? undefined}
            placeholder="执行环境"
            onChange={(value) => setEnvironmentId(Number(value))}
            options={environments.map((e) => ({ label: e.name, value: e.id }))}
          />
        </Space>

        <Space style={{ marginBottom: 12 }} wrap>
          <Input style={{ width: 360 }} value={name} onChange={setName} placeholder="用例名称" />
          <Select
            style={{ width: 180 }}
            value={moduleId ?? 0}
            placeholder="选择模块"
            onChange={(value) => setModuleId(Number(value) ? Number(value) : null)}
            options={[
              { label: '未分组', value: 0 },
              ...modules.map((m) => ({ label: m.name, value: m.id })),
            ]}
          />
          <Select
            style={{ width: 120 }}
            value={priority}
            onChange={setPriority}
            options={[
              { label: '高', value: 'high' },
              { label: '中', value: 'medium' },
              { label: '低', value: 'low' },
            ]}
          />
          <Input style={{ width: 200 }} value={tags} onChange={setTags} placeholder="标签（逗号分隔）" />
        </Space>
        <Input.TextArea
          value={description}
          onChange={setDescription}
          placeholder="用例描述（可选）"
          autoSize={{ minRows: 1, maxRows: 3 }}
          style={{ marginBottom: 12 }}
        />

        <div style={{ display: 'flex', gap: 16 }}>
          <Card style={{ width: 260, flex: '0 0 260px', alignSelf: 'flex-start' }} bodyStyle={{ padding: 8 }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 8 }}>
              <Text bold>步骤（{steps.length}）</Text>
              <Button size="mini" icon={<IconPlus />} onClick={addStep}>添加</Button>
            </div>
            {steps.map((step, index) => (
              <div
                key={index}
                role="button"
                tabIndex={0}
                onClick={() => setActiveIndex(index)}
                onKeyDown={(e) => e.key === 'Enter' && setActiveIndex(index)}
                style={{
                  padding: '8px 10px',
                  marginBottom: 6,
                  borderRadius: 4,
                  cursor: 'pointer',
                  background: index === activeIndex ? 'var(--color-primary-light-1)' : 'var(--color-fill-1)',
                  border: index === activeIndex ? '1px solid var(--color-primary-light-4)' : '1px solid transparent',
                }}
              >
                <div style={{ fontSize: 13 }}>{index + 1}. {step.name || '未命名步骤'}</div>
                <div style={{ fontSize: 11, color: 'var(--color-text-3)', marginTop: 2 }}>
                  {step.request?.method} {step.request?.url}
                </div>
              </div>
            ))}
            <Space size="mini" style={{ marginTop: 8 }}>
              <Button size="mini" icon={<IconUp />} disabled={activeIndex === 0} onClick={() => moveStep(-1)} />
              <Button size="mini" icon={<IconDown />} disabled={activeIndex === steps.length - 1} onClick={() => moveStep(1)} />
              <Button size="mini" icon={<IconCopy />} onClick={duplicateStep} />
              <Button size="mini" status="danger" icon={<IconDelete />} onClick={deleteStep} />
            </Space>
          </Card>

          <div style={{ flex: 1, minWidth: 0 }}>
            <Card bodyStyle={{ padding: 12 }} style={{ marginBottom: 12 }}>
              <Space wrap>
                <Text>从接口定义填充当前步骤：</Text>
                <Select
                  style={{ width: 280 }}
                  showSearch
                  value={pickDefinitionId ?? undefined}
                  placeholder="选择接口定义"
                  onChange={(value) => setPickDefinitionId(Number(value))}
                  options={definitions.map((d) => ({ label: `${d.method} ${d.path} · ${d.name}`, value: d.id }))}
                />
                <Button onClick={fillFromDefinition}>填充</Button>
              </Space>
            </Card>
            <Card bodyStyle={{ padding: 12 }}>
              <RequestEditor
                spec={current}
                definitionName={definitions.find((d) => d.id === current.definition_id)?.name}
                onChange={updateCurrent}
                onSend={handleSend}
                sending={sending}
                projectId={projectId}
                responseSample={response ? {
                  method: response.rendered?.method,
                  url: response.rendered?.url,
                  status_code: response.response?.status,
                  headers: response.response?.headers,
                  body_text: response.response?.body_preview,
                } : null}
              />
            </Card>
            {response && (
              <Card style={{ marginTop: 12 }} bodyStyle={{ padding: 12 }}>
                <Title heading={6}>响应结果</Title>
                <ResponsePanel response={response} loading={sending} />
              </Card>
            )}
          </div>
        </div>
      </Card>

      <Drawer
        title="变更记录"
        visible={revisionsVisible}
        onCancel={() => setRevisionsVisible(false)}
        width={720}
        footer={null}
      >
        <Table
          rowKey="id"
          loading={revisionsLoading}
          pagination={false}
          data={revisions}
          columns={[
            { title: '时间', dataIndex: 'created_at', width: 170, render: (v: string) => v ? new Date(v).toLocaleString('zh-CN') : '—' },
            { title: '来源', dataIndex: 'source', width: 90, render: (v: string) => <Tag color={v === 'chat' ? 'arcoblue' : 'green'}>{v === 'chat' ? '对话' : '页面'}</Tag> },
            { title: '摘要', dataIndex: 'summary', render: (v: string) => v || '—' },
            {
              title: '操作',
              width: 90,
              render: (_: unknown, row: any) =>
                row.before?.api_spec ? (
                  <Button size="mini" onClick={() => Modal.confirm({
                    title: '恢复历史版本',
                    content: '将用该版本覆盖当前接口用例（名称/优先级/请求步骤），确认继续？',
                    onOk: () => restoreRevision(row.id),
                  })}>恢复</Button>
                ) : <Text type="secondary" style={{ fontSize: 12 }}>不可恢复</Text>,
            },
          ]}
        />
      </Drawer>
    </div>
  );
};

export default ApiCaseEditorPage;
