export type CaseKind = 'functional' | 'ui';

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

export interface Environment {
  id: number;
  name: string;
  base_url: string;
  browser: string;
  headless: boolean;
  is_default: boolean;
  project_id: number;
  cookies?: Array<{ name: string; value: string; domain?: string }>;
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
