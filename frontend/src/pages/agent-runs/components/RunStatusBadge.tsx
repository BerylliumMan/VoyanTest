import React from 'react';
import { Tag } from '@arco-design/web-react';
import { AgentRunStatus } from '../types';
import useLocale from '@/utils/useLocale';

/** 6 种运行状态到 Arco Tag 颜色的映射 */
const STATUS_COLOR_MAP: Record<AgentRunStatus, string> = {
  pending: 'gray',
  running: 'blue',
  paused: 'orange',
  completed: 'green',
  failed: 'red',
  cancelled: 'magenta',
};

/** 状态标签映射 -- 用于获取 i18n 键后缀 */
const STATUS_KEY_MAP: Record<AgentRunStatus, string> = {
  pending: 'pending',
  running: 'running',
  paused: 'paused',
  completed: 'completed',
  failed: 'failed',
  cancelled: 'cancelled',
};

export interface RunStatusBadgeProps {
  status: AgentRunStatus;
}

const RunStatusBadge: React.FC<RunStatusBadgeProps> = ({ status }) => {
  const t = useLocale();
  const color = STATUS_COLOR_MAP[status] || 'gray';
  const labelKey = `agent_runs.status.${STATUS_KEY_MAP[status] || status}`;

  return <Tag color={color}>{t[labelKey] || status}</Tag>;
};

export default RunStatusBadge;
