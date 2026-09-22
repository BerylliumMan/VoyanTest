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
  post: unknown[];
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
  body: { type: 'none', content: '' },
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
});