// 报告页共享类型定义
// 契约：specs/029-api-testing/data-model.md §5 —— report.json 的 steps[] 是前端 StepDetail 的超集，
// API 用例额外携带 request/response/assertions/extracted（均为可选，老报告无这些字段时行为不变）。

/** 请求头：后端可能给数组 [{key,value}]（api_spec 风格）或对象 {name: value}（响应头风格） */
export type HeadersLike = Record<string, string> | Array<{ key?: string; value?: string }> | undefined;

/** 接口用例步骤的请求明细 */
export interface ApiRequestDetail {
  method?: string;
  url?: string;
  headers?: HeadersLike;
  body?: unknown;
}

/** 接口用例步骤的响应明细 */
export interface ApiResponseDetail {
  status?: number;
  headers?: HeadersLike;
  body?: unknown;
  duration_ms?: number;
  size?: number;
}

/** 接口用例步骤的断言结果 */
export interface ApiAssertionDetail {
  name?: string;
  type?: string;
  condition?: string;
  expected?: string;
  actual?: string;
  passed?: boolean;
  error?: string;
}

/** 接口用例步骤的提取变量结果（value 已是后端脱敏后的值，前端不再处理） */
export interface ApiExtractedDetail {
  variable?: string;
  value?: string;
  scope?: string;
  type?: string;
  ok?: boolean;
  error?: string;
}

/** 执行步骤（UI 用例与 API 用例共用；API 扩展字段均为可选超集） */
export interface StepDetail {
  step_number?: number;
  description?: string;
  original_description?: string;
  status?: string;
  success?: boolean;
  error?: string;
  action?: string;
  screenshot_path?: string;
  // API 用例扩展字段（可选，老报告缺失时渲染路径与原来完全一致）
  request?: ApiRequestDetail;
  response?: ApiResponseDetail;
  assertions?: ApiAssertionDetail[];
  extracted?: ApiExtractedDetail[];
}

/** 单条用例执行记录 */
export interface RunItem {
  id: number;
  run_id: number;
  case_id: number;
  case_name: string;
  status: string;
  duration: number;
  started_at: string;
  finished_at: string;
  steps: StepDetail[];
  logs?: string;
}