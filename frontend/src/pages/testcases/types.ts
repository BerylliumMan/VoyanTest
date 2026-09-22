export type CaseKind = 'functional' | 'ui' | 'api';

export interface TestCase {
  id: number;
  project_case_number: number;
  name: string;
  description: string;
  module_id: number | null;
  is_init: boolean;
  case_kind?: CaseKind;
  compiled_script?: string | null;
  compiled_script_hash?: string | null;
  compiled_at?: string | null;
  steps: { step_order: number; description: string; parsed_result?: string; retry_max?: number; retry_delay?: number; healed_selector?: string; learned_locator?: Record<string, unknown> | null; structured_step?: Record<string, unknown> | null; cacheable?: boolean }[];
}

export interface Module {
  id: number;
  project_id: number;
  name: string;
  parent_id: number | null;
  children?: Module[];
}

export interface EnvVariable {
  key: string;
  value: string;
  secret?: boolean;
  enable?: boolean;
  /** 仅 GET 回显：secret 项在库中是否真有值（value 恒为 "******"） */
  has_value?: boolean;
}

export interface EnvHeader {
  key: string;
  value: string;
  enable?: boolean;
}

export interface Environment {
  id: number;
  name: string;
  base_url: string;
  browser: string;
  headless: boolean;
  is_default: boolean;
  project_id: number;
  cookies?: Array<{ name: string; value: string; domain?: string }>;
  variables?: EnvVariable[];
  headers?: EnvHeader[];
}

export interface Agent {
  name: string;
  status: string;
}

export interface Step {
  step_order: number;
  description: string;
  parsed_result?: string;
  retry_max?: number;
  retry_delay?: number;
  healed_selector?: string;
  learned_locator?: Record<string, unknown> | null;
  structured_step?: Record<string, unknown> | null;
  /** When false, skip locator/plan cache replay & learning for this step */
  cacheable?: boolean;
}

export interface Project {
  id: number;
  name: string;
}
