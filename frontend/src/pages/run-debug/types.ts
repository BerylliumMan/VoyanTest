// run-debug 页面类型定义

export type StepStatus = 'pending' | 'running' | 'passed' | 'failed' | 'skipped';

export interface StepInfo {
  id?: number;
  step_order: number;
  description: string;
  healed_selector?: string;
  status: StepStatus;
  duration?: number;
  error?: string;
  screenshot_path?: string;
  logs: string[];
}

export interface CaseData {
  id: number;
  name: string;
  description?: string;
  steps: {
    id: number;
    step_order: number;
    description: string;
    healed_selector?: string;
  }[];
}

export interface WsStepStart {
  type: 'step_start';
  timestamp: string;
  step_id: number;
  message: string;
}

export interface WsStepComplete {
  type: 'step_complete';
  timestamp: string;
  step_id: number;
  status: string;
  duration: number;
}

export interface WsExecutionPaused {
  type: 'execution_paused';
  run_id: number;
  step_id: number;
  step_description: string;
  reason: string;
  options: string[];
}

export interface WsExecutionResumed {
  type: 'execution_resumed';
  run_id: number;
  step_id: number;
  decision: string;
  new_description?: string;
}

export type WsMessage = WsStepStart | WsStepComplete | WsExecutionPaused | WsExecutionResumed;

export type ExecutionPhase = 'idle' | 'running' | 'paused' | 'completed';
