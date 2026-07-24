import React, { useState } from 'react';
import { Tag, Spin, Typography, Button, Tooltip } from '@arco-design/web-react';
import { IconCode, IconLoading } from '@arco-design/web-react/icon';
import { OTATurn } from '../types';
import useLocale from '@/utils/useLocale';
import styles from '../style/index.module.less';

const { Text, Paragraph } = Typography;

/** 角色标签颜色映射 */
const ROLE_COLOR_MAP: Record<string, string> = {
  user: 'blue',
  assistant: 'green',
  tool: 'orange',
  system: 'gray',
};

export interface OTATimelineProps {
  turns: OTATurn[];
  /** 是否显示 WS 实时连接指示器 */
  live?: boolean;
  /** 当前是否正在处理中（显示 loading spinner） */
  active?: boolean;
}

/** 单条工具调用展示 */
const ToolCallItem: React.FC<{ call: OTATurn['tool_calls'][number] }> = ({ call }) => {
  const [expanded, setExpanded] = useState(false);
  const t = useLocale();

  const statusColor = call.status === 'success' ? 'green' : call.status === 'error' ? 'red' : 'gray';
  const argsStr = call.arguments ? JSON.stringify(call.arguments, null, 2) : '{}';
  const resultStr = call.result || '';

  return (
    <div className={styles['tool-call-item']}>
      <div className={styles['tool-call-header']} onClick={() => setExpanded(!expanded)}>
        <IconCode style={{ marginRight: 6 }} />
        <Text bold>{call.name}</Text>
        <Tag size="small" color={statusColor} style={{ marginLeft: 8 }}>
          {call.status}
        </Tag>
        <Button
          type="text"
          size="mini"
          className={styles['tool-call-expand-btn']}
        >
          {expanded ? t['close'] || '收起' : t['detail'] || '展开'}
        </Button>
      </div>
      {expanded && (
        <div className={styles['tool-call-body']}>
          {call.arguments && (
            <div className={styles['tool-call-section']}>
              <Text type="secondary" style={{ fontSize: 12 }}>
                {t['agent_runs.turn.tool_args'] || 'Arguments'}
              </Text>
              <pre className={styles['tool-call-pre']}>{argsStr}</pre>
            </div>
          )}
          {resultStr && (
            <div className={styles['tool-call-section']}>
              <Text type="secondary" style={{ fontSize: 12 }}>
                {t['agent_runs.turn.tool_result'] || 'Result'}
              </Text>
              <pre className={styles['tool-call-pre']}>{resultStr}</pre>
            </div>
          )}
          {call.error && (
            <div className={styles['tool-call-section']}>
              <Text type="error" style={{ fontSize: 12 }}>
                {t['agent_runs.turn.tool_error'] || 'Error'}
              </Text>
              <pre className={`${styles['tool-call-pre']} ${styles['tool-call-error']}`}>
                {call.error}
              </pre>
            </div>
          )}
        </div>
      )}
    </div>
  );
};

/** Turn-by-Turn 时间线组件 */
const OTATimeline: React.FC<OTATimelineProps> = ({ turns, live = false, active = false }) => {
  const t = useLocale();
  const [expandedTurns, setExpandedTurns] = useState<Set<string>>(new Set());

  const toggleExpand = (turnId: string) => {
    setExpandedTurns((prev) => {
      const next = new Set(prev);
      if (next.has(turnId)) {
        next.delete(turnId);
      } else {
        next.add(turnId);
      }
      return next;
    });
  };

  if (!turns.length && !active) {
    return (
      <div className={styles['timeline-empty']}>
        <Text type="secondary">{t['agent_runs.turn.no_turns'] || '暂无对话记录'}</Text>
      </div>
    );
  }

  return (
    <div className={styles['ota-timeline']}>
      {/* 实时连接状态 */}
      {live && (
        <div className={styles['timeline-live-bar']}>
          <span className={styles['timeline-live-dot']} />
          <Text style={{ fontSize: 12, marginLeft: 6 }}>
            {active
              ? t['agent_runs.turn.receiving'] || '接收实时数据...'
              : t['agent_runs.turn.live_connected'] || '实时连接已建立'}
          </Text>
          {active && <Spin size={14} style={{ marginLeft: 8 }} />}
        </div>
      )}

      {turns.map((turn, idx) => {
        const isLast = idx === turns.length - 1;
        const isExpanded = expandedTurns.has(turn.id);
        const roleColor = ROLE_COLOR_MAP[turn.role] || 'gray';
        const roleLabel = t[`agent_runs.turn.role.${turn.role}`] || turn.role;
        const contentPreview = turn.content?.length > 200
          ? `${turn.content.slice(0, 200)}...`
          : turn.content;

        return (
          <div
            key={turn.id}
            className={`${styles['timeline-item']} ${isLast ? styles['timeline-item-last'] : ''}`}
          >
            {/* 时间线节点 */}
            <div className={styles['timeline-dot-column']}>
              <div className={styles['timeline-dot']} style={{ background: `var(--color-${roleColor}-6, #165DFF)` }} />
              {!isLast && <div className={styles['timeline-line']} />}
            </div>

            {/* 内容区 */}
            <div className={styles['timeline-content']}>
              {/* Turn 头部 */}
              <div className={styles['turn-header']}>
                <Tag size="small" color={roleColor}>
                  {roleLabel}
                </Tag>
                <Text type="secondary" style={{ fontSize: 12, marginLeft: 8 }}>
                  Turn #{turn.turn_number}
                </Text>
                <Text type="secondary" style={{ fontSize: 11, marginLeft: 'auto' }}>
                  {turn.timestamp ? new Date(turn.timestamp).toLocaleTimeString() : ''}
                </Text>
                {/* 最后一个 turn 且正在活跃 -- loading 指示器 */}
                {isLast && active && (
                  <IconLoading spin style={{ marginLeft: 8, fontSize: 14, color: 'var(--color-blue-6)' }} />
                )}
              </div>

              {/* 内容 */}
              <div
                className={`${styles['turn-content']} ${isExpanded ? styles['turn-content-expanded'] : ''}`}
              >
                <Paragraph
                  ellipsis={!isExpanded ? { rows: 3, expandable: false } : false}
                  style={{ whiteSpace: 'pre-wrap', margin: 0 }}
                >
                  {turn.content || t['agent_runs.turn.empty_content'] || '(空)'}
                </Paragraph>
              </div>

              {/* 展开/收起按钮 */}
              {turn.content?.length > 200 && (
                <Button
                  type="text"
                  size="mini"
                  className={styles['turn-expand-btn']}
                  onClick={() => toggleExpand(turn.id)}
                >
                  {isExpanded
                    ? t['close'] || '收起'
                    : t['detail'] || '展开全部'}
                </Button>
              )}

              {/* 工具调用列表 */}
              {turn.tool_calls?.length > 0 && (
                <div className={styles['tool-calls-section']}>
                  <Text type="secondary" style={{ fontSize: 12, marginBottom: 8, display: 'block' }}>
                    {t['agent_runs.turn.tool_calls'] || '工具调用'} ({turn.tool_calls.length})
                  </Text>
                  {turn.tool_calls.map((call) => (
                    <ToolCallItem key={call.id} call={call} />
                  ))}
                </div>
              )}
            </div>
          </div>
        );
      })}
    </div>
  );
};

export default OTATimeline;
