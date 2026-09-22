import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useHistory } from 'react-router-dom';
import {
  Button,
  Card,
  Dropdown,
  Empty,
  Form,
  Input,
  Menu,
  Message,
  Modal,
  Popconfirm,
  Select,
  Spin,
  Tag,
  Tooltip,
  Tree,
  Typography,
} from '@arco-design/web-react';
import {
  IconCopy,
  IconDelete,
  IconEdit,
  IconFolder,
  IconFolderAdd,
  IconImport,
  IconPlayArrow,
  IconPlus,
  IconRefresh,
  IconSave,
  IconSettings,
  IconStorage,
  IconSwap,
  IconThunderbolt,
} from '@arco-design/web-react/icon';
import { apiDelete, apiGet, apiPost, apiPut } from '@/utils/apiRequest';
import {
  ApiDefinition,
  ApiDefinitionDetail,
  ApiSpec,
  ApiStep,
  Dataset,
  DatasetBinding,
  DebugResponse,
  Environment,
  HTTP_METHODS,
  Module,
  Project,
  createStep,
} from './types';
import DefinitionDrawer from './components/DefinitionDrawer';
import DatasetPanel from './components/DatasetPanel';
import EnvVariablesModal from './components/EnvVariablesModal';
import RequestEditor from './components/RequestEditor';
import ResponsePanel from './components/ResponsePanel';
import styles from './style/index.module.less';

const { Title, Text } = Typography;
const FormItem = Form.Item;

interface TreeNodeData {
  key: string;
  title: React.ReactNode;
  children?: TreeNodeData[];
  isLeaf?: boolean;
}

interface ApiCaseSummary {
  id: number;
  case_kind?: string;
  api_spec?: ApiSpec | null;
}

interface SavedCase {
  id: number;
  signature: string;
}

const METHOD_TAG_COLOR: Record<string, string> = {
  GET: 'green',
  POST: 'arcoblue',
  PUT: 'orange',
  PATCH: 'purple',
  DELETE: 'red',
  HEAD: 'gray',
  OPTIONS: 'gray',
};

/** 从 request_schema 生成默认请求模板 */
const buildStepFromDefinition = (def: ApiDefinitionDetail): ApiStep => {
  const step = createStep();
  step.name = def.name || def.path;
  step.definition_id = def.id;
  step.request.method = def.method;
  step.request.url = `{{baseUrl}}${def.path}`;

  const params = def.request_schema?.params || [];
  // query 参数
  step.request.query = params
    .filter((p) => p.in === 'query')
    .map((p) => ({
      key: p.name,
      value: p.example !== undefined ? String(p.example) : '',
      enable: true,
    }));
  // path 参数 → 替换进 URL
  const pathParams = params.filter((p) => p.in === 'path');
  if (pathParams.length > 0) {
    let url = step.request.url;
    pathParams.forEach((p) => {
      const example = p.example !== undefined ? String(p.example) : `{${p.name}}`;
      url = url.replace(`{${p.name}}`, example);
    });
    step.request.url = url;
  }

  // body
  const body = def.request_schema?.body;
  if (body && body.content_type) {
    const ct = body.content_type.toLowerCase();
    if (ct.includes('json')) {
      step.request.body = {
        type: 'json',
        content: body.example !== undefined
          ? (typeof body.example === 'string' ? body.example : JSON.stringify(body.example, null, 2))
          : '{}',
      };
      step.request.headers = [
        { key: 'Content-Type', value: 'application/json', enable: true },
      ];
    } else if (ct.includes('x-www-form-urlencoded')) {
      step.request.body = { type: 'form', content: '' };
    } else if (ct.includes('multipart')) {
      step.request.body = { type: 'form_data', content: '' };
    } else {
      step.request.body = {
        type: 'raw',
        content: body.example !== undefined ? String(body.example) : '',
      };
    }
  }

  return step;
};

const DEFAULT_DATASET_BINDING: DatasetBinding = {
  dataset_id: null,
  dataset_mode: 'sequential',
};

const specSignature = (spec: ApiSpec): string => JSON.stringify(spec);

/** 「未分组」虚拟分组节点的 key（非真实模块） */
const UNGROUPED_KEY = 'ungrouped';
/** 分组树节点 key 前缀（真实模块） */
const MODULE_KEY_PREFIX = 'module-';
/** 接口定义叶子节点 key 前缀 */
const DEF_KEY_PREFIX = 'def-';

/** 扁平模块列表 → 模块树（后端 /modules 返回扁平列表，含 parent_id） */
const buildModuleTree = (flat: Module[]): Module[] => {
  const byId = new Map<number, Module>();
  flat.forEach((m) => byId.set(m.id, { ...m, children: [] }));
  const roots: Module[] = [];
  byId.forEach((m) => {
    const parent = m.parent_id != null ? byId.get(m.parent_id) : undefined;
    if (parent) parent.children!.push(m);
    else roots.push(m);
  });
  return roots;
};

const ApiTestPage: React.FC = () => {
  const history = useHistory();
  const [projects, setProjects] = useState<Project[]>([]);
  const [projectId, setProjectId] = useState<number | null>(null);
  const [environments, setEnvironments] = useState<Environment[]>([]);
  const [environmentId, setEnvironmentId] = useState<number | null>(null);
  const [modules, setModules] = useState<Module[]>([]);
  const [definitions, setDefinitions] = useState<ApiDefinition[]>([]);
  const [defLoading, setDefLoading] = useState(false);
  const [selectedDef, setSelectedDef] = useState<ApiDefinition | null>(null);
  /** 当前分组：选中分组节点或接口所属分组；新建接口落入此分组（null = 未分组） */
  const [selectedModuleId, setSelectedModuleId] = useState<number | null>(null);

  const [step, setStep] = useState<ApiStep>(createStep());
  const [response, setResponse] = useState<DebugResponse | null>(null);
  const [sending, setSending] = useState(false);
  const [dryRun, setDryRun] = useState(false);

  const [drawerVisible, setDrawerVisible] = useState(false);
  const [datasets, setDatasets] = useState<Dataset[]>([]);
  const [datasetPanelVisible, setDatasetPanelVisible] = useState(false);
  const [datasetBinding, setDatasetBinding] =
    useState<DatasetBinding>(DEFAULT_DATASET_BINDING);

  const [defModalVisible, setDefModalVisible] = useState(false);
  const [defForm] = Form.useForm();
  const [savingDef, setSavingDef] = useState(false);

  const [expandedKeys, setExpandedKeys] = useState<string[]>([]);
  const knownModuleKeysRef = useRef<Set<string>>(new Set());

  const [moduleModalVisible, setModuleModalVisible] = useState(false);
  const [moduleForm] = Form.useForm();
  const [editingModule, setEditingModule] = useState<Module | null>(null);
  const [moduleParentId, setModuleParentId] = useState<number | null>(null);
  const [savingModule, setSavingModule] = useState(false);

  const [envModalVisible, setEnvModalVisible] = useState(false);

  const [saveCaseVisible, setSaveCaseVisible] = useState(false);
  const [saveCaseForm] = Form.useForm();
  const [saveCaseLoading, setSaveCaseLoading] = useState(false);
  const [running, setRunning] = useState(false);
  const [savedCases, setSavedCases] = useState<Record<number, SavedCase>>({});

  // ---- 项目 ----
  useEffect(() => {
    apiGet<Project[]>('/api/projects/')
      .then((data) => {
        setProjects(data || []);
        if (data && data.length > 0) {
          setProjectId(data[0].id);
        }
      })
      .catch(() => setProjects([]));
  }, []);

  // ---- 环境 ----
  const loadEnvironments = useCallback(async () => {
    if (!projectId) {
      setEnvironments([]);
      setEnvironmentId(null);
      return;
    }
    try {
      const data = await apiGet<Environment[]>(`/api/projects/${projectId}/environments`);
      const envs = data || [];
      setEnvironments(envs);
      setEnvironmentId((prev) =>
        prev != null && envs.some((e) => e.id === prev)
          ? prev
          : ((envs.find((e) => e.is_default) || envs[0])?.id ?? null)
      );
    } catch {
      setEnvironments([]);
      setEnvironmentId(null);
    }
  }, [projectId]);

  useEffect(() => {
    loadEnvironments();
  }, [loadEnvironments]);

  const selectedEnvironment = useMemo(
    () => environments.find((e) => e.id === environmentId) || null,
    [environments, environmentId]
  );

  // ---- 模块 + 定义 ----
  const loadDefinitions = useCallback(async () => {
    if (!projectId) return;
    setDefLoading(true);
    try {
      const [mods, defs] = await Promise.all([
        apiGet<Module[]>(`/api/projects/${projectId}/modules`).catch(() => []),
        apiGet<{ total: number; items: ApiDefinition[] }>('/api/api-test/definitions', {
          project_id: projectId,
          page_size: 500,
        }).catch(() => ({ total: 0, items: [] })),
      ]);
      setModules(mods || []);
      setDefinitions(defs.items || []);
    } finally {
      setDefLoading(false);
    }
  }, [projectId]);

  useEffect(() => {
    loadDefinitions();
  }, [loadDefinitions]);

  useEffect(() => {
    setSelectedModuleId(null);
    setSelectedDef(null);
  }, [projectId]);

  // ---- 已存在的接口用例（用于「执行用例」复用） ----
  const loadApiCases = useCallback(async () => {
    if (!projectId) {
      setSavedCases({});
      return;
    }
    try {
      const data = await apiGet<{ items?: ApiCaseSummary[] }>('/api/testcases/search', {
        project_id: projectId,
        q: '',
        page: 1,
        size: 200,
        case_kind: 'api',
      });
      const map: Record<number, SavedCase> = {};
      (data?.items || [])
        .filter((c) => c.case_kind === 'api' && c.api_spec)
        .forEach((c) => {
          const defId = c.api_spec?.steps?.[0]?.definition_id;
          if (typeof defId === 'number') {
            map[defId] = { id: c.id, signature: specSignature(c.api_spec as ApiSpec) };
          }
        });
      setSavedCases(map);
    } catch {
      setSavedCases({});
    }
  }, [projectId]);

  useEffect(() => {
    loadApiCases();
  }, [loadApiCases]);

  useEffect(() => {
    setSelectedDef(null);
    setStep(createStep());
    setResponse(null);
  }, [projectId]);

  // ---- 数据集 ----
  const loadDatasets = useCallback(async () => {
    if (!projectId) {
      setDatasets([]);
      return;
    }
    try {
      const data = await apiGet<{ items?: Dataset[] } | Dataset[]>('/api/api-test/datasets', { project_id: projectId });
      // 契约：分页形状 {total, items}；兼容裸数组（历史/未来变更）
      setDatasets(Array.isArray(data) ? data : (data?.items ?? []));
    } catch {
      setDatasets([]);
    }
  }, [projectId]);

  useEffect(() => {
    loadDatasets();
  }, [loadDatasets]);

  useEffect(() => {
    setDatasetBinding((prev) =>
      prev.dataset_id != null && !datasets.some((d) => d.id === prev.dataset_id)
        ? { ...prev, dataset_id: null }
        : prev
    );
  }, [datasets]);

  // ---- 点击定义 → 载入模板 ----
  const handleSelectDefinition = async (key: string) => {
    if (!key.startsWith('def-')) return;
    const id = Number(key.slice(4));
    try {
      const detail = await apiGet<ApiDefinitionDetail>(`/api/api-test/definitions/${id}`);
      setSelectedDef(detail);
      setStep(buildStepFromDefinition(detail));
      setResponse(null);
    } catch {
      Message.error('加载接口定义失败');
    }
  };

  // ---- 新增 / 删除接口定义 ----
  const selectedModule = useMemo(
    () => (selectedModuleId == null ? null : modules.find((m) => m.id === selectedModuleId) || null),
    [modules, selectedModuleId]
  );

  const openCreateDefinition = () => {
    defForm.resetFields();
    defForm.setFieldsValue({ method: 'GET' });
    setDefModalVisible(true);
  };

  const handleCreateDefinition = async () => {
    if (!projectId) return;
    let values: { name: string; method: string; path: string; summary?: string };
    try {
      values = await defForm.validate();
    } catch {
      return;
    }
    setSavingDef(true);
    try {
      const created = await apiPost<ApiDefinition>(
        '/api/api-test/definitions',
        {
          project_id: projectId,
          module_id: selectedModuleId,
          name: values.name,
          method: values.method,
          path: values.path,
          summary: values.summary || null,
        },
        selectedModule ? `接口已新增到「${selectedModule.name}」` : '接口已新增（未分组）'
      );
      setDefModalVisible(false);
      defForm.resetFields();
      await loadDefinitions();
      if (created?.id) {
        await handleSelectDefinition(`${DEF_KEY_PREFIX}${created.id}`);
      }
    } catch {
      /* 409 等错误 detail 已由 apiRequest 弹出 */
    } finally {
      setSavingDef(false);
    }
  };

  const handleDeleteDefinition = async (def: ApiDefinition) => {
    try {
      await apiDelete(`/api/api-test/definitions/${def.id}`, '接口已删除');
      if (selectedDef?.id === def.id) {
        setSelectedDef(null);
        setStep(createStep());
        setResponse(null);
      }
      await loadDefinitions();
      loadApiCases();
    } catch {
      /* apiRequest 已弹错误 */
    }
  };

  const handleCopyDefinition = async (def: ApiDefinition) => {
    try {
      const created = await apiPost<ApiDefinition>(
        `/api/api-test/definitions/${def.id}/copy`,
        {},
        '接口已复制'
      );
      await loadDefinitions();
      if (created?.id) handleSelectDefinition(`def-${created.id}`);
    } catch {
      /* apiRequest 已弹错误 */
    }
  };

  // ---- 分组数据（分组 = 模块 modules） ----
  const moduleTree = useMemo(() => buildModuleTree(modules), [modules]);
  const moduleIdSet = useMemo(() => new Set(modules.map((m) => m.id)), [modules]);

  const currentModuleIdOf = (def: ApiDefinition): number | null =>
    def.module_id != null && moduleIdSet.has(def.module_id) ? def.module_id : null;

  const defsByModule = useMemo(() => {
    const map = new Map<number, ApiDefinition[]>();
    definitions.forEach((d) => {
      const mid = d.module_id != null && moduleIdSet.has(d.module_id) ? d.module_id : null;
      if (mid == null) return;
      if (!map.has(mid)) map.set(mid, []);
      map.get(mid)!.push(d);
    });
    return map;
  }, [definitions, moduleIdSet]);

  const ungroupedDefs = useMemo(
    () => definitions.filter((d) => currentModuleIdOf(d) == null),
    // currentModuleIdOf 由 moduleIdSet 决定，依赖其即可
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [definitions, moduleIdSet]
  );

  const moduleOptions = useMemo(() => {
    const opts: Array<{ id: number; name: string; depth: number }> = [];
    const walk = (list: Module[], depth: number) => {
      list.forEach((m) => {
        opts.push({ id: m.id, name: m.name, depth });
        if (m.children) walk(m.children, depth + 1);
      });
    };
    walk(moduleTree, 0);
    return opts;
  }, [moduleTree]);

  // 模块集合变化时：保留用户已展开状态，自动展开新出现的分组，清理已删除的 key
  useEffect(() => {
    const keys: string[] = [];
    const walk = (list: Module[]) => {
      list.forEach((m) => {
        keys.push(`${MODULE_KEY_PREFIX}${m.id}`);
        if (m.children) walk(m.children);
      });
    };
    walk(buildModuleTree(modules));
    const known = knownModuleKeysRef.current;
    const additions = keys.filter((k) => !known.has(k));
    knownModuleKeysRef.current = new Set(keys);
    setExpandedKeys((prev) => {
      const valid = prev.filter((k) => keys.includes(k));
      const extra = additions.filter((k) => !valid.includes(k));
      if (extra.length === 0 && valid.length === prev.length) return prev;
      return [...valid, ...extra];
    });
  }, [modules]);

  // ---- 分组增删改 ----
  const openCreateModule = (parentId: number | null) => {
    setEditingModule(null);
    setModuleParentId(parentId);
    moduleForm.resetFields();
    setModuleModalVisible(true);
  };

  const openRenameModule = (mod: Module) => {
    setEditingModule(mod);
    setModuleParentId(mod.parent_id);
    moduleForm.resetFields();
    moduleForm.setFieldsValue({ name: mod.name });
    setModuleModalVisible(true);
  };

  const handleSubmitModule = async () => {
    if (!projectId) return;
    let values: { name: string };
    try {
      values = await moduleForm.validate();
    } catch {
      return;
    }
    const name = values.name.trim();
    if (!name) {
      Message.error('分组名称不能为空');
      return;
    }
    setSavingModule(true);
    try {
      if (editingModule) {
        await apiPut(`/api/modules/${editingModule.id}`, { name }, '分组已重命名');
      } else {
        await apiPost(
          `/api/projects/${projectId}/modules`,
          { project_id: projectId, name, parent_id: moduleParentId },
          '分组已创建'
        );
      }
      setModuleModalVisible(false);
      moduleForm.resetFields();
      await loadDefinitions();
    } catch {
      /* apiRequest 已弹错误（如 400 循环引用） */
    } finally {
      setSavingModule(false);
    }
  };

  const handleDeleteModule = async (mod: Module) => {
    try {
      await apiDelete(`/api/modules/${mod.id}`, '分组已删除');
    } catch {
      // 409（分组/子分组下有接口或用例）等 detail 已由 apiRequest 原样弹出，保留分组
      return;
    }
    if (editingModule?.id === mod.id) setModuleModalVisible(false);
    if (selectedModuleId === mod.id) setSelectedModuleId(null);
    await loadDefinitions();
  };

  const handleMoveDefinition = async (def: ApiDefinition, targetModuleId: number | null) => {
    if (currentModuleIdOf(def) === targetModuleId) return;
    try {
      await apiPut(
        `/api/api-test/definitions/${def.id}`,
        { module_id: targetModuleId },
        '接口已移动'
      );
      await loadDefinitions();
    } catch {
      /* apiRequest 已弹错误 */
    }
  };

  // ---- 接口树（按模块分组，支持子分组） ----
  const renderDefNode = (d: ApiDefinition): TreeNodeData => ({
    key: `${DEF_KEY_PREFIX}${d.id}`,
    isLeaf: true,
    title: (
      <span className={styles.treeDefRow}>
        <span className={styles.treeDefMain}>
          <span className={styles.treeDefName}>{d.name}</span>
          <span className={styles.treeDefMeta}>
            <Tag
              color={METHOD_TAG_COLOR[d.method] || 'arcoblue'}
              size="small"
              className={styles.treeMethodTag}
            >
              {d.method}
            </Tag>
            <span className={styles.treeDefPath}>{d.path}</span>
          </span>
        </span>
        <span className={styles.treeDefActions}>
          <Dropdown
            trigger="click"
            position="bl"
            droplist={
              <Menu
                onClickMenuItem={(menuKey) =>
                  handleMoveDefinition(
                    d,
                    menuKey === UNGROUPED_KEY ? null : Number(menuKey)
                  )
                }
              >
                {moduleOptions.map((o) => (
                  <Menu.Item key={String(o.id)}>
                    {`${'　'.repeat(o.depth)}${o.name}`}
                  </Menu.Item>
                ))}
                <Menu.Item key={UNGROUPED_KEY}>未分组</Menu.Item>
              </Menu>
            }
          >
            <Button
              className={styles.treeMoveBtn}
              size="mini"
              type="text"
              icon={<IconSwap />}
              onClick={(e) => e.stopPropagation()}
              aria-label="移动到分组"
            />
          </Dropdown>
          <Popconfirm title={`复制接口「${d.name}」？`} onOk={() => handleCopyDefinition(d)}>
            <Button
              className={styles.treeCopyBtn}
              size="mini"
              type="text"
              icon={<IconCopy />}
              onClick={(e) => e.stopPropagation()}
              aria-label="复制接口"
            />
          </Popconfirm>
          <Popconfirm title={`删除接口「${d.name}」？`} onOk={() => handleDeleteDefinition(d)}>
            <Button
              className={styles.treeDeleteBtn}
              size="mini"
              type="text"
              status="danger"
              icon={<IconDelete />}
              onClick={(e) => e.stopPropagation()}
              aria-label="删除接口"
            />
          </Popconfirm>
        </span>
      </span>
    ),
  });

  const renderModuleTitle = (mod: Module, count: number): React.ReactNode => (
    <span className={styles.treeModuleRow}>
      <span className={styles.treeModuleMain}>
        <IconFolder className={styles.treeModuleIcon} />
        <span className={styles.treeModuleName}>{mod.name}</span>
        <span className={styles.treeCount}>{count}</span>
      </span>
      <span className={styles.treeModuleActions}>
        <Tooltip content="新建子分组">
          <Button
            size="mini"
            type="text"
            icon={<IconFolderAdd />}
            onClick={(e) => {
              e.stopPropagation();
              openCreateModule(mod.id);
            }}
            aria-label="新建子分组"
          />
        </Tooltip>
        <Tooltip content="重命名">
          <Button
            size="mini"
            type="text"
            icon={<IconEdit />}
            onClick={(e) => {
              e.stopPropagation();
              openRenameModule(mod);
            }}
            aria-label="重命名分组"
          />
        </Tooltip>
        <Tooltip content="删除">
          <Button
            size="mini"
            type="text"
            status="danger"
            icon={<IconDelete />}
            onClick={(e) => {
              e.stopPropagation();
              Modal.confirm({
                title: '删除分组',
                content: `确定删除分组「${mod.name}」？分组下的接口会阻止删除，需先移出（移动到其他分组或未分组）。`,
                okButtonProps: { status: 'danger' },
                onOk: () => handleDeleteModule(mod),
              });
            }}
            aria-label="删除分组"
          />
        </Tooltip>
      </span>
    </span>
  );

  const buildModuleNode = (mod: Module): { node: TreeNodeData; count: number } => {
    const ownDefs = defsByModule.get(mod.id) || [];
    const childModules = (mod.children || []).map(buildModuleNode);
    const count = ownDefs.length + childModules.reduce((sum, c) => sum + c.count, 0);
    const node: TreeNodeData = {
      key: `${MODULE_KEY_PREFIX}${mod.id}`,
      title: renderModuleTitle(mod, count),
      children: [...childModules.map((c) => c.node), ...ownDefs.map(renderDefNode)],
    };
    return { node, count };
  };

  const treeData: TreeNodeData[] = [
    ...moduleTree.map((m) => buildModuleNode(m).node),
    ...(ungroupedDefs.length > 0
      ? [
          {
            key: UNGROUPED_KEY,
            title: (
              <span className={styles.treeModuleRow}>
                <span className={styles.treeModuleMain}>
                  <IconFolder className={styles.treeModuleIcon} />
                  <span className={styles.treeModuleName}>未分组</span>
                  <span className={styles.treeCount}>{ungroupedDefs.length}</span>
                </span>
              </span>
            ),
            children: ungroupedDefs.map(renderDefNode),
          } as TreeNodeData,
        ]
      : []),
  ];

  // ---- api_spec 顶层：步骤 + 数据集绑定（dataset_id / dataset_mode） ----
  const apiSpec = useMemo<ApiSpec>(
    () => ({
      schema_version: 1,
      variables: [],
      dataset_id: datasetBinding.dataset_id,
      dataset_mode: datasetBinding.dataset_mode,
      fail_policy: 'fail_fast',
      steps: [step],
    }),
    [step, datasetBinding]
  );

  const currentSignature = useMemo(() => specSignature(apiSpec), [apiSpec]);

  const defaultCaseName = () =>
    selectedDef?.name || step.name || step.request.url || '接口用例';

  const createCase = async (name: string): Promise<number> => {
    const created = await apiPost<{ id: number }>('/api/testcases/', {
      project_id: projectId,
      module_id: null,
      name: name.trim() || '接口用例',
      description: '',
      case_kind: 'api',
      api_spec: apiSpec,
      steps: [],
    });
    const defId = step.definition_id;
    if (defId != null) {
      setSavedCases((prev) => ({ ...prev, [defId]: { id: created.id, signature: currentSignature } }));
    }
    return created.id;
  };

  const findReusableCase = (): number | null => {
    const defId = step.definition_id;
    if (defId == null) return null;
    const rec = savedCases[defId];
    return rec && rec.signature === currentSignature ? rec.id : null;
  };

  const openSaveCase = () => {
    if (!projectId) {
      Message.warning('请先选择项目');
      return;
    }
    saveCaseForm.setFieldsValue({ name: defaultCaseName() });
    setSaveCaseVisible(true);
  };

  const handleSubmitSaveCase = async () => {
    let values: { name: string };
    try {
      values = await saveCaseForm.validate();
    } catch {
      return;
    }
    setSaveCaseLoading(true);
    try {
      const id = await createCase(values.name);
      Message.success(`已保存为用例 #${id}`);
      setSaveCaseVisible(false);
      loadApiCases();
    } catch {
      /* apiRequest 已弹错误 */
    } finally {
      setSaveCaseLoading(false);
    }
  };

  const handleRunCase = async () => {
    if (!projectId) {
      Message.warning('请先选择项目');
      return;
    }
    if (!environmentId) {
      Message.warning('请先选择环境');
      return;
    }
    setRunning(true);
    try {
      let caseId = findReusableCase();
      if (caseId == null) {
        caseId = await createCase(defaultCaseName());
      }
      const res = await apiPost<{ batch_id: number }>('/api/testcases/batch-run', {
        case_ids: [caseId],
        environment_id: environmentId,
      });
      Message.success({
        duration: 6000,
        content: (
          <span>
            已触发运行（批次 #{res.batch_id}、用例 #{caseId}）
            <Button
              type="text"
              size="mini"
              onClick={() => history.push('/reports')}
              style={{ marginLeft: 4 }}
            >
              查看报告
            </Button>
          </span>
        ),
      });
    } catch {
      /* apiRequest 已弹错误 */
    } finally {
      setRunning(false);
    }
  };

  // ---- 发送 / 校验 ----
  const handleSend = async () => {
    if (!projectId) {
      Message.warning('请先选择项目');
      return;
    }
    setSending(true);
    try {
      const data = await apiPost<DebugResponse>(
        '/api/api-test/debug',
        {
          environment_id: environmentId,
          case_variables: [],
          step,
          api_spec: apiSpec,
          dry_run: dryRun,
        },
        undefined
      );
      setResponse(data);
      if (dryRun && data.error) {
        Message.error(data.error);
      }
    } catch {
      /* apiRequest 已弹错误 */
    } finally {
      setSending(false);
    }
  };

  return (
    <div className={styles.page}>
      {/* 顶部工具栏 */}
      <div className={styles.toolbar}>
        <div className={styles.toolbarLeft}>
          <Select
            placeholder="选择项目"
            value={projectId ?? undefined}
            onChange={(v) => setProjectId(typeof v === 'number' ? v : null)}
            options={projects.map((p) => ({ label: p.name, value: p.id }))}
            showSearch
            className={styles.projectSelect}
          />
          <Select
            placeholder="选择环境"
            value={environmentId ?? undefined}
            onChange={(v) => setEnvironmentId(typeof v === 'number' ? v : null)}
            options={environments.map((e) => ({ label: e.name, value: e.id }))}
            disabled={!projectId || environments.length === 0}
            allowClear
            className={styles.envSelect}
          />
          <Button
            icon={<IconSettings />}
            onClick={() => setEnvModalVisible(true)}
            disabled={!environmentId}
          >
            环境变量
          </Button>
        </div>
        <div className={styles.toolbarRight}>
          <Button icon={<IconSave />} onClick={openSaveCase} disabled={!projectId}>
            保存为用例
          </Button>
          <Button
            type="primary"
            icon={<IconPlayArrow />}
            onClick={handleRunCase}
            loading={running}
            disabled={!projectId || !environmentId}
          >
            执行用例
          </Button>
          <Button
            icon={<IconStorage />}
            onClick={() => setDatasetPanelVisible(true)}
            disabled={!projectId}
          >
            数据集
          </Button>
          <Button
            icon={<IconImport />}
            onClick={() => setDrawerVisible(true)}
            disabled={!projectId}
          >
            导入文档
          </Button>
          <Button
            type="outline"
            icon={<IconThunderbolt />}
            onClick={() => setDrawerVisible(true)}
            disabled={!projectId}
          >
            生成用例
          </Button>
        </div>
      </div>

      {/* 三栏布局 */}
      <div className={styles.layout}>
        {/* 左：接口树 */}
        <Card className={styles.leftPanel} bodyStyle={{ padding: 8 }}>
          <div className={styles.leftHeader}>
            <Title heading={6} style={{ margin: 0 }}>
              接口列表
            </Title>
            <div className={styles.leftHeaderActions}>
              <Tooltip content="新建分组">
                <Button
                  size="mini"
                  type="text"
                  icon={<IconFolderAdd />}
                  onClick={() => openCreateModule(null)}
                  disabled={!projectId}
                  aria-label="新建分组"
                />
              </Tooltip>
              <Button
                size="mini"
                type="text"
                icon={<IconPlus />}
                onClick={openCreateDefinition}
                disabled={!projectId}
                aria-label="新增接口"
              />
              <Button
                size="mini"
                type="text"
                icon={<IconRefresh />}
                onClick={loadDefinitions}
                loading={defLoading}
                aria-label="刷新"
              />
            </div>
          </div>
          <Spin loading={defLoading} style={{ width: '100%' }} className={styles.leftSpin}>
            {modules.length === 0 && definitions.length === 0 ? (
              <Empty description="暂无接口，请先导入文档或手动新增" />
            ) : (
              <Tree
                treeData={treeData}
                expandedKeys={expandedKeys}
                onExpand={(keys) => setExpandedKeys(keys)}
                onSelect={(keys) => {
                  const key = String(keys[0] || '');
                  if (key.startsWith(DEF_KEY_PREFIX)) {
                    const def = definitions.find(
                      (d) => d.id === Number(key.slice(DEF_KEY_PREFIX.length))
                    );
                    setSelectedModuleId(def?.module_id ?? null);
                    handleSelectDefinition(key);
                  } else if (key.startsWith(MODULE_KEY_PREFIX)) {
                    setSelectedModuleId(Number(key.slice(MODULE_KEY_PREFIX.length)) || null);
                  } else if (key === UNGROUPED_KEY) {
                    setSelectedModuleId(null);
                  }
                }}
                draggable
                allowDrop={(info) => {
                  const dragKey = info.dragNode?.props?._key || '';
                  const dropKey = info.dropNode?.props?._key || '';
                  if (!dragKey.startsWith(DEF_KEY_PREFIX)) return false;
                  return dropKey.startsWith(MODULE_KEY_PREFIX) || dropKey === UNGROUPED_KEY;
                }}
                onDrop={(info) => {
                  const dragKey = info.dragNode?.props?._key || '';
                  const dropKey = info.dropNode?.props?._key || '';
                  if (!dragKey.startsWith(DEF_KEY_PREFIX)) return;
                  const def = definitions.find(
                    (d) => d.id === Number(dragKey.slice(DEF_KEY_PREFIX.length))
                  );
                  if (!def) return;
                  if (dropKey === UNGROUPED_KEY) {
                    handleMoveDefinition(def, null);
                  } else if (dropKey.startsWith(MODULE_KEY_PREFIX)) {
                    handleMoveDefinition(def, Number(dropKey.slice(MODULE_KEY_PREFIX.length)));
                  }
                }}
                blockNode
                className={styles.defTree}
              />
            )}
          </Spin>
        </Card>

        {/* 中：请求编辑器 */}
        <Card className={styles.middlePanel} bodyStyle={{ padding: 12 }}>
          <RequestEditor
            spec={step}
            definitionName={selectedDef?.name}
            onChange={setStep}
            onSend={handleSend}
            sending={sending}
            dryRun={dryRun}
            onDryRunChange={setDryRun}
            datasets={datasets}
            datasetBinding={datasetBinding}
            onDatasetBindingChange={setDatasetBinding}
          />
        </Card>

        {/* 右：响应面板 */}
        <Card className={styles.rightPanel} bodyStyle={{ padding: 12 }}>
          <div className={styles.rightHeader}>
            <Title heading={6} style={{ margin: 0 }}>
              响应
            </Title>
            {response && (
              <Text type="secondary" style={{ fontSize: 12 }}>
                {new Date().toLocaleTimeString()}
              </Text>
            )}
          </div>
          <ResponsePanel response={response} loading={sending} />
        </Card>
      </div>

      <DefinitionDrawer
        visible={drawerVisible}
        onClose={() => setDrawerVisible(false)}
        projectId={projectId}
        onImported={loadDefinitions}
        onGenerated={() => {
          loadDefinitions();
          loadApiCases();
        }}
      />

      <DatasetPanel
        visible={datasetPanelVisible}
        onClose={() => setDatasetPanelVisible(false)}
        projectId={projectId}
        onChanged={loadDatasets}
      />

      <EnvVariablesModal
        visible={envModalVisible}
        onClose={() => setEnvModalVisible(false)}
        environment={selectedEnvironment}
        onSaved={loadEnvironments}
      />

      <Modal
        title={selectedModule ? `新增接口（${selectedModule.name}）` : '新增接口（未分组）'}
        visible={defModalVisible}
        onOk={handleCreateDefinition}
        confirmLoading={savingDef}
        onCancel={() => setDefModalVisible(false)}
        unmountOnExit
      >
        <Form form={defForm} layout="vertical">
          <FormItem
            field="name"
            label="接口名称"
            rules={[{ required: true, message: '请输入接口名称' }]}
          >
            <Input placeholder="如 获取用户列表" />
          </FormItem>
          <FormItem field="method" label="请求方法" initialValue="GET">
            <Select options={HTTP_METHODS.map((m) => ({ label: m, value: m }))} />
          </FormItem>
          <FormItem
            field="path"
            label="接口路径"
            rules={[
              { required: true, message: '请输入接口路径' },
              { match: /^\//, message: '路径必须以 / 开头' },
            ]}
          >
            <Input placeholder="/api/users" />
          </FormItem>
          <FormItem field="summary" label="摘要（可选）">
            <Input.TextArea placeholder="接口用途简述" autoSize={{ minRows: 2, maxRows: 4 }} />
          </FormItem>
        </Form>
      </Modal>

      <Modal
        title="保存为用例"
        visible={saveCaseVisible}
        onOk={handleSubmitSaveCase}
        confirmLoading={saveCaseLoading}
        onCancel={() => setSaveCaseVisible(false)}
        unmountOnExit
      >
        <Form form={saveCaseForm} layout="vertical">
          <FormItem
            field="name"
            label="用例名称"
            rules={[{ required: true, message: '请输入用例名称' }]}
          >
            <Input placeholder="用例名称" />
          </FormItem>
        </Form>
      </Modal>

      <Modal
        title={
          editingModule
            ? '重命名分组'
            : moduleParentId != null
            ? '新建子分组'
            : '新建分组'
        }
        visible={moduleModalVisible}
        onOk={handleSubmitModule}
        confirmLoading={savingModule}
        onCancel={() => setModuleModalVisible(false)}
        unmountOnExit
      >
        <Form form={moduleForm} layout="vertical">
          <FormItem
            field="name"
            label="分组名称"
            rules={[
              { required: true, message: '请输入分组名称' },
              { maxLength: 255, message: '分组名称最长 255 字符' },
            ]}
          >
            <Input
              placeholder="如 用户中心"
              autoFocus
              onPressEnter={handleSubmitModule}
            />
          </FormItem>
        </Form>
      </Modal>
    </div>
  );
};

export default ApiTestPage;
