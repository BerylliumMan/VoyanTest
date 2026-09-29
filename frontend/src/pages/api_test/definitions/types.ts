/**
 * 接口测试（029-api-testing）类型定义。
 * 严格对齐 specs/029-api-testing/data-model.md §3 的 api_spec JSONB 契约。
 */

/** 键值对（headers/query/变量/环境头等通用） */
export interface KeyValueItem {
  key: string;
  value: string;
  enable: boolean;
  /** 标记为敏感值（UI 打码显示） */
  secret?: boolean;
}

/** 请求体类型 */
export type BodyType = 'none' | 'json' | 'form' | 'form_data' | 'raw' | 'binary';

/** 请求体 */
export interface ApiRequestBody {
  type: BodyType;
  content: string;
  /** 031（US1）multipart：普通字段与文件字段（path 为 platform://<file_id> 或变量引用） */
  form_data?: Array<{ key: string; value: string; enable?: boolean }>;
  files?: Array<{ key: string; path: string; content_type?: string | null }>;
}

/** 认证配置 */
export interface ApiAuth {
  type: 'none' | 'basic' | 'bearer' | 'api_key';
  /** basic: {username, password}；bearer: {token}；api_key: {key, value, in} */
  [key: string]: unknown;
}

/** 单步请求配置（data-model §3 request） */
export interface ApiRequestSpec {
  method: string;
  url: string;
  headers: KeyValueItem[];
  query: KeyValueItem[];
  body: ApiRequestBody;
  auth: ApiAuth;
  timeout_ms: number;
  follow_redirects: boolean;
  verify_ssl: boolean;
}

/** 断言（data-model §3 assertions[]） */
export interface ApiAssertion {
  enable: boolean;
  type: string;
  condition: string;
  expected: string;
  name: string;
  /** jsonpath/header/body_regex 等需要表达式 */
  expression?: string;
}

/** 提取器（data-model §3 extractors[]） */
export interface ApiExtractor {
  enable: boolean;
  type: string;
  expression: string;
  variable: string;
  scope: 'case' | 'environment';
  required: boolean;
}

/** 前置操作（set_variable / delay） */
export interface ApiPreStep {
  type: 'set_variable' | 'delay';
  key?: string;
  value?: string;
  ms?: number;
}

/** 数据集迭代模式（api_spec.dataset_mode） */
export type DatasetMode = 'sequential' | 'random' | 'loop';

/** 迭代模式下拉选项 */
export const DATASET_MODES: Array<{ label: string; value: DatasetMode }> = [
  { label: '顺序', value: 'sequential' },
  { label: '随机', value: 'random' },
  { label: '循环', value: 'loop' },
];

/** 数据集绑定（api_spec 顶层字段） */
export interface DatasetBinding {
  dataset_id: number | null;
  dataset_mode: DatasetMode;
  /** 031（US9）：loop 模式重复遍数（1~100） */
  loop_count?: number;
  /** 031（US9）：迭代失败策略（fail_fast = 立即停止，默认） */
  fail_policy?: 'fail_fast' | 'continue';
}

/** 数据集 Select 的「不使用」哨兵值（真实 id 从 1 起） */
export const DATASET_NONE_VALUE = 0;

/** 行数上限（契约：≤1000 行） */
export const DATASET_ROW_LIMIT = 1000;

/** CSV 文件大小上限（契约：≤2MB） */
export const DATASET_IMPORT_MAX_BYTES = 2 * 1024 * 1024;

/** 单步（data-model §3 steps[]） */
export interface ApiStep {
  order: number;
  name: string;
  enable: boolean;
  definition_id: number | null;
  request: ApiRequestSpec;
  assertions: ApiAssertion[];
  extractors: ApiExtractor[];
  pre: ApiPreStep[];
  /** 后置操作，结构与前置相同：设置变量 / 延时 */
  post: ApiPreStep[];
  /** 031（US3）前置/后置脚本（Python 子集，服务端沙箱执行） */
  pre_script?: string;
  post_script?: string;
  /** 031（US3）步骤级脚本超时（200~10000ms；null/undefined = 系统默认 2000ms） */
  script_timeout_ms?: number | null;
  /** 031（US5）重试策略；null/undefined = 默认（网络/超时 × 1，5xx 与断言不重试） */
  retry?: RetryPolicy | null;
}

/** 031（US5）重试策略：on 缺省 = ["timeout","network"]（5xx 需显式加入） */
export interface RetryPolicy {
  max: number;
  delay_ms: number;
  on?: Array<'5xx' | 'timeout' | 'network'>;
}

/** 031（US1）测试文件（平台托管） */
export interface TestFile {
  id: number;
  name: string;
  size: number;
  content_type?: string | null;
  project_id?: number | null;
  ref_count: number;
  created_at?: string | null;
}

/** 031（US7）调试请求历史 */
export interface RequestHistory {
  id: number;
  method: string;
  url: string;
  status_code: number | null;
  duration_ms: number | null;
  created_at: string | null;
  headers_masked: Record<string, string>;
  body_preview: string | null;
  error: string | null;
}

/** 031（US8）CI 令牌 */
export interface ApiToken {
  id: number;
  name: string;
  token_prefix: string;
  project_id: number | null;
  expires_at: string | null;
  revoked_at: string | null;
  last_used_at: string | null;
  created_at: string | null;
  /** 仅创建响应返回一次 */
  token?: string;
}

/** 031（US11）AI 断言/提取器候选 */
export interface SuggestedAssertion {
  kind: 'assertion' | 'extractor';
  type: string;
  expression: string;
  condition: string;
  expected: string;
  variable: string;
  scope: string;
  reason: string;
}

/** 031（US11）覆盖率条目（双口径信号） */
export interface CoverageItem {
  definition_id: number;
  name: string;
  method: string;
  path: string;
  covered: boolean;
  case_count: number;
  last_run_passed: boolean | null;
  priority_score: number;
}

/** api_spec 顶层（data-model §3） */
export interface ApiSpec {
  schema_version: number;
  variables: KeyValueItem[];
  dataset_id: number | null;
  dataset_mode?: DatasetMode;
  fail_policy: 'fail_fast' | 'continue';
  steps: ApiStep[];
}

/** 接口定义列表项（contract §1.2） */
export interface ApiDefinition {
  id: number;
  module_id: number | null;
  name: string;
  method: string;
  path: string;
  summary: string | null;
  tags: string | null;
  operation_id: string | null;
}

/** 接口定义详情（含 schema） */
export interface ApiDefinitionDetail extends ApiDefinition {
  request_schema?: {
    params: Array<{
      name: string;
      in: string;
      type: string;
      required: boolean;
      example?: unknown;
      enum?: unknown[];
      schema?: unknown;
    }>;
    body: {
      content_type: string;
      schema?: unknown;
      example?: unknown;
    };
  };
  response_schema?: {
    statuses: Record<string, { schema?: unknown; example?: unknown }>;
  } | null;
  source?: string;
}

/** 导入结果（contract §1.1） */
export interface ImportResult {
  import_id: number;
  source: string;
  total_operations: number;
  created: number;
  updated: number;
  skipped: number;
  operations: Array<{ id: number; method: string; path: string; name: string }>;
}

/** 导入历史项 */
export interface ImportHistoryItem {
  id: number;
  file_name: string;
  source: string;
  total_operations: number;
  created_count: number;
  updated_count: number;
  skipped_count: number;
  error: string | null;
  created_at: string;
}

/** 生成草案（contract §1.3） */
export interface GeneratedCase {
  draft_id: string;
  definition_id: number;
  name: string;
  priority: string;
  api_spec: ApiSpec;
  notes?: string;
}

/** 生成会话响应 */
export interface GenerateResponse {
  session_id: string;
  total: number;
  cases: GeneratedCase[];
}

/** 调试执行响应（contract §1.4） */
export interface DebugResponse {
  rendered: {
    method: string;
    url: string;
    headers: Record<string, string>;
    body_preview: string;
  };
  response: {
    status: number;
    duration_ms: number;
    size: number;
    headers: Record<string, string>;
    body_preview: string;
    truncated: boolean;
  } | null;
  assertions: Array<{
    name: string;
    passed: boolean;
    expected: string;
    actual: string;
    error?: string;
  }>;
  extracted: Array<{ variable: string; value_masked: string }>;
  error: string | null;
}

/** 项目 */
export interface Project {
  id: number;
  name: string;
}

/** 数据集（contracts §1.7）：rows 为 {列名: 值} 对象数组 */
export interface Dataset {
  id: number;
  project_id?: number;
  name: string;
  columns: string[];
  rows: Array<Record<string, unknown>>;
  source?: 'manual' | 'csv';
  created_at: string;
  updated_at?: string;
}

/** 数据集新建/更新载荷 */
export interface DatasetPayload {
  project_id?: number;
  name: string;
  columns: string[];
  rows: Array<Record<string, unknown>>;
}

/** CSV 解析结果（POST /datasets/import，不落库） */
export interface DatasetImportResult {
  columns: string[];
  rows: Array<Record<string, unknown>>;
}

/** 环境变量项（GET 回显：secret 项 value 恒为 "******" + has_value） */
export interface EnvVariableItem {
  key: string;
  value: string;
  secret?: boolean;
  enable?: boolean;
  has_value?: boolean;
}

/** 环境公共请求头项 */
export interface EnvHeaderItem {
  key: string;
  value: string;
  enable?: boolean;
}

/** 环境（列表接口 GET /api/projects/{id}/environments 已含 variables/headers，secret 已打码） */
export interface Environment {
  id: number;
  name: string;
  base_url: string;
  is_default?: boolean;
  project_id?: number;
  browser?: string;
  headless?: boolean;
  cookies?: Array<{ name: string; value: string; domain?: string }>;
  variables?: EnvVariableItem[];
  headers?: EnvHeaderItem[];
  /** 031（US4）多域名服务地址：{服务名: 绝对 URL} */
  services?: Record<string, string>;
}

/** 模块 */
export interface Module {
  id: number;
  project_id: number;
  name: string;
  parent_id: number | null;
  children?: Module[];
}

/** 生成选项（contract §1.3） */
export interface GenerateOptions {
  normal: boolean;
  boundary: boolean;
  missing_required: boolean;
  type_error: boolean;
  auth_fail: boolean;
  max_cases_per_operation: number;
  use_llm: boolean;
}

/** 默认生成选项 */
export const DEFAULT_GENERATE_OPTIONS: GenerateOptions = {
  normal: true,
  boundary: true,
  missing_required: true,
  type_error: false,
  auth_fail: false,
  max_cases_per_operation: 5,
  use_llm: true,
};

/** 断言类型选项 */
export const ASSERTION_TYPES = [
  'expression',
  'status_code',
  'jsonpath',
  'header',
  'body_contains',
  'body_regex',
  'response_time',
] as const;

/** 断言比较条件 */
export const ASSERTION_CONDITIONS = [
  'equals',
  'not_equals',
  'contains',
  'not_contains',
  'gt',
  'gte',
  'lt',
  'lte',
  'exists',
  'not_exists',
  'regex',
] as const;

/** 提取器类型 */
export const EXTRACTOR_TYPES = ['jsonpath', 'regex', 'header', 'cookie'] as const;

/** 请求方法 */
export const HTTP_METHODS = ['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD', 'OPTIONS'] as const;

/** 请求体类型 */
export const BODY_TYPES: BodyType[] = ['none', 'json', 'form', 'form_data', 'raw', 'binary'];

/** 新建空断言 */
export const createAssertion = (): ApiAssertion => ({
  enable: true,
  type: 'status_code',
  condition: 'equals',
  expected: '200',
  name: '',
});

/** 新建空提取器 */
export const createExtractor = (): ApiExtractor => ({
  enable: true,
  type: 'jsonpath',
  expression: '',
  variable: '',
  scope: 'case',
  required: true,
});

/** 新建空键值项 */
export const createKeyValue = (): KeyValueItem => ({
  key: '',
  value: '',
  enable: true,
  secret: false,
});

/** 新建空请求配置 */
export const createRequestSpec = (): ApiRequestSpec => ({
  method: 'GET',
  url: '{{baseUrl}}/',
  headers: [],
  query: [],
  body: { type: 'none', content: '', form_data: [], files: [] },
  auth: { type: 'none' },
  timeout_ms: 30000,
  follow_redirects: true,
  verify_ssl: true,
});

/** 新建空步骤 */
export const createStep = (): ApiStep => ({
  order: 1,
  name: '请求',
  enable: true,
  definition_id: null,
  request: createRequestSpec(),
  assertions: [],
  extractors: [],
  pre: [],
  post: [],
  pre_script: '',
  post_script: '',
  retry: null,
});

/** 031（US6）断言模板库：一键插入常用断言（FR-012） */
export interface AssertionTemplate {
  key: string;
  label: string;
  hint: string;
  build: () => ApiAssertion;
}

export const ASSERTION_TEMPLATES: AssertionTemplate[] = [
  {
    key: 'status_2xx',
    label: '状态码 2xx',
    hint: '200 <= status_code < 300',
    build: () => ({
      ...createAssertion(),
      type: 'expression',
      condition: 'expression',
      expression: '200 <= status_code < 300',
      name: '状态码 2xx',
    }),
  },
  {
    key: 'response_time',
    label: '响应时间小于 1000ms',
    hint: 'response_time ≤ 1000',
    build: () => ({
      ...createAssertion(),
      type: 'response_time',
      condition: 'lte',
      expected: '1000',
      name: '响应时间 < 1s',
    }),
  },
  {
    key: 'jsonpath_exists',
    label: 'JSONPath 存在',
    hint: '如 $.data.token（改字段名）',
    build: () => ({
      ...createAssertion(),
      type: 'jsonpath',
      condition: 'exists',
      expression: '$.data.token',
      expected: '',
      name: '字段存在',
    }),
  },
  {
    key: 'body_contains',
    label: '响应体包含文本',
    hint: '如 success（改文本）',
    build: () => ({
      ...createAssertion(),
      type: 'body_contains',
      condition: 'contains',
      expected: 'success',
      name: '响应包含关键词',
    }),
  },
  {
    key: 'json_schema',
    label: 'JSON Schema 校验',
    hint: '粘贴 schema（改内容）',
    build: () => ({
      ...createAssertion(),
      type: 'jsonschema',
      condition: 'equals',
      expression: '{"type": "object", "required": ["data"]}',
      expected: '',
      name: 'Schema 校验',
    }),
  },
  {
    key: 'expression_cross_field',
    label: '表达式：跨字段一致',
    hint: "len(json['data']['items']) == json['data']['total']",
    build: () => ({
      ...createAssertion(),
      type: 'expression',
      condition: 'expression',
      expression: "len(json['data']['items']) == json['data']['total']",
      name: '跨字段一致',
    }),
  },
];
