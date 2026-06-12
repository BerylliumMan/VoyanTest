export interface TestCase {
  id: number;
  project_case_number: number;
  name: string;
  description: string;
  module_id: number | null;
  is_init: boolean;
  steps: { step_order: number; description: string; parsed_result?: string }[];
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
}

export interface Project {
  id: number;
  name: string;
}
