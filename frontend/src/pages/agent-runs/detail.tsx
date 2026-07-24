import React, { useMemo } from 'react';
import { useParams, useHistory } from 'react-router-dom';
import {
  Card,
  Spin,
  Typography,
  Button,
  Breadcrumb,
} from '@arco-design/web-react';
import { IconLeft } from '@arco-design/web-react/icon';
import useLocale from '@/utils/useLocale';
import useAgentRun from './hooks/useAgentRun';
import RunStatusBadge from './components/RunStatusBadge';
import OTATimeline from './components/OTATimeline';
import styles from './style/index.module.less';

const { Title, Text } = Typography;

/** 格式化耗时 */
const formatDuration = (seconds: number | null): string => {
  if (seconds === null || seconds === undefined) return '-';
  if (seconds < 60) return `${seconds}s`;
  const min = Math.floor(seconds / 60);
  const sec = seconds % 60;
  return `${min}m ${sec}s`;
};

/** 格式化时间 */
const formatTime = (ts: string | null): string => {
  if (!ts) return '-';
  return new Date(ts).toLocaleString();
};

const AgentRunDetail: React.FC = () => {
  const { runId } = useParams<{ runId: string }>();
  const history = useHistory();
  const t = useLocale();
  const { run, turns, wsStatus, loading } = useAgentRun(runId);

  /** WS 是否活跃（run 正在运行且 WS 已连接） */
  const isWsActive = useMemo(
    () => run?.status === 'running' || wsStatus === 'connected' || wsStatus === 'connecting',
    [run?.status, wsStatus]
  );

  /** 是否有 turn 正在流式接收中 */
  const isTurnInProgress = useMemo(
    () => run?.status === 'running' && wsStatus === 'connected',
    [run?.status, wsStatus]
  );

  /** 返回列表 */
  const handleBack = () => {
    history.push('/agent-runs');
  };

  /** 渲染错误信息 */
  const renderError = () => {
    if (!run?.error_message) return null;
    return (
      <div className={styles['run-error-message']}>
        <Text type="error" bold>
          {t['agent_runs.detail.error_message'] || '错误信息'}
        </Text>
        <pre>{run.error_message}</pre>
      </div>
    );
  };

  if (loading) {
    return (
      <div className={styles['agent-run-detail-page']}>
        <div className={styles['run-info-loading']}>
          <Spin tip={t['loading'] || '加载中...'} />
        </div>
      </div>
    );
  }

  if (!run) {
    return (
      <div className={styles['agent-run-detail-page']}>
        <Card>
          <div className={styles['run-detail-error']}>
            <Text>{t['agent_runs.detail.not_found'] || '运行记录未找到'}</Text>
            <Button type="primary" onClick={handleBack}>
              {t['back.to.reports'] || '返回列表'}
            </Button>
          </div>
        </Card>
      </div>
    );
  }

  return (
    <div className={styles['agent-run-detail-page']}>
      {/* 面包屑导航 */}
      <Breadcrumb style={{ marginBottom: 16 }}>
        <Breadcrumb.Item onClick={handleBack} style={{ cursor: 'pointer' }}>
          {t['agent_runs.list.title'] || 'Agent 运行记录'}
        </Breadcrumb.Item>
        <Breadcrumb.Item>
          {t['agent_runs.detail.breadcrumb'] || '运行详情'} #{run.id}
        </Breadcrumb.Item>
      </Breadcrumb>

      {/* 运行信息头部 */}
      <div className={styles['run-info-header']}>
        <Card className={styles['run-info-card']}>
          <div className={styles['run-info-grid']}>
            <div className={styles['run-info-item']}>
              <div className={styles['run-info-label']}>
                {t['agent_runs.col.agent'] || 'Agent'}
              </div>
              <div className={styles['run-info-value']}>
                {run.agent_definition_name || `#${run.agent_definition_id}`}
              </div>
            </div>
            <div className={styles['run-info-item']}>
              <div className={styles['run-info-label']}>
                {t['status'] || '状态'}
              </div>
              <div className={styles['run-info-value']}>
                <RunStatusBadge status={run.status} />
              </div>
            </div>
            <div className={styles['run-info-item']}>
              <div className={styles['run-info-label']}>
                {t['agent_runs.col.turns'] || '轮次'}
              </div>
              <div className={styles['run-info-value']}>
                {turns.length || run.turns_used || '-'}
              </div>
            </div>
            <div className={styles['run-info-item']}>
              <div className={styles['run-info-label']}>
                {t['agent_runs.col.duration'] || '耗时'}
              </div>
              <div className={styles['run-info-value']}>
                {formatDuration(run.duration_ms)}
              </div>
            </div>
            <div className={`${styles['run-info-item']} ${styles['run-info-goal']}`}>
              <div className={styles['run-info-label']}>
                {t['agent_runs.col.goal'] || '目标'}
              </div>
              <div className={styles['run-info-value']}>
                {typeof run.goal === 'string' ? run.goal : JSON.stringify(run.goal || {})}
              </div>
            </div>
          </div>

          {/* 时间信息 */}
          <div style={{ display: 'flex', gap: 24, marginTop: 12, fontSize: 12, color: 'var(--color-text-3)' }}>
            <span>
              {t['agent_runs.col.started'] || '开始'}: {formatTime(run.started_at)}
            </span>
            <span>
              {t['agent_runs.detail.finished'] || '结束'}: {formatTime(run.completed_at)}
            </span>
          </div>
        </Card>
      </div>

      {/* 错误信息 */}
      {renderError()}

      {/* Turn-by-Turn 时间线 */}
      <Card
        title={t['agent_runs.detail.timeline_title'] || '执行过程'}
        style={{ marginTop: 16 }}
        extra={
          <Button type="text" size="small" icon={<IconLeft />} onClick={handleBack}>
            {t['back.to.reports'] || '返回列表'}
          </Button>
        }
      >
        <OTATimeline
          turns={turns}
          live={wsStatus === 'connected' || wsStatus === 'connecting'}
          active={isTurnInProgress}
        />
      </Card>
    </div>
  );
};

export default AgentRunDetail;
