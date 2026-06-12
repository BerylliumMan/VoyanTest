import React from 'react';
import { Modal, Descriptions, Card, Tag, Button } from '@arco-design/web-react';
import useLocale from '@/utils/useLocale';
import styles from './style/index.module.less';

interface StepDetail {
  step_number?: number;
  description?: string;
  original_description?: string;
  status?: string;
  success?: boolean;
  error?: string;
  action?: string;
  screenshot_path?: string;
}

interface RunItem {
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

interface RunDetailProps {
  visible: boolean;
  run: RunItem | null;
  onClose: () => void;
}

const STATUS_COLORS: Record<string, string> = {
  pending: 'blue',
  running: 'blue',
  passed: 'green',
  failed: 'red',
  skipped: 'orange',
};

const RunDetail: React.FC<RunDetailProps> = ({ visible, run, onClose }) => {
  const t = useLocale();

  const STATUS_LABELS: Record<string, string> = {
    pending: t['step.waiting'],
    running: t['running'],
    passed: t['passed'],
    failed: t['failed'],
    skipped: 'Skipped',
  };

  const getStatusTag = (s: string) => (
    <Tag color={STATUS_COLORS[s] || 'blue'}>
      {STATUS_LABELS[s] || s}
    </Tag>
  );

  return (
    <Modal
      visible={visible}
      onCancel={onClose}
      title={run ? run.case_name : ''}
      footer={<Button onClick={onClose}>{t['close']}</Button>}
      style={{ width: 800 }}
    >
      {run && (
        <div>
          <Descriptions
            column={2}
            data={[
              { label: t['status'], value: getStatusTag(run.status) },
              { label: t['duration'], value: run.duration ? `${run.duration.toFixed(1)}s` : '--' },
              { label: t['start.time'], value: run.started_at ? new Date(run.started_at).toLocaleString() : '--' },
              { label: t['end.time'], value: run.finished_at ? new Date(run.finished_at).toLocaleString() : '--' },
            ]}
            style={{ marginBottom: 24 }}
          />
          {run.steps && run.steps.length > 0 ? (
            <div>
              <h4 className={styles['run-section-title']}>{t['step.detail']}</h4>
              {run.steps.map((step: StepDetail, idx: number) => (
                <Card key={idx} className={styles['step-card']}
                  bodyStyle={{ padding: 12 }}
                >
                  <div className={styles['step-header']}>
                    <Tag style={{ width: 28, textAlign: 'center' }}>
                      {step.step_number || idx + 1}
                    </Tag>
                    <span className={styles['step-description']}>{step.original_description || step.description}</span>
                    {step.status && getStatusTag(
                      step.status === 'skipped' ? 'skipped' :
                      step.success ? 'passed' : 'failed'
                    )}
                  </div>
                  {step.error && (
                    <div className={styles['step-error']}>
                      {step.error}
                    </div>
                  )}
                  {step.action && (
                    <div className={styles['step-detail']}>
                      &gt; {step.action}
                    </div>
                  )}
                  {step.screenshot_path && (
                    <div style={{ marginTop: 8 }}>
                      <img
                        src={`/${step.screenshot_path}`}
                        alt={t['screenshot'].replace('{num}', step.step_number)}
                        className={styles['step-screenshot']}
                      />
                    </div>
                  )}
                </Card>
              ))}
            </div>
          ) : (
            <div className={styles['step-empty']}>{t['no.steps']}</div>
          )}
        </div>
      )}
    </Modal>
  );
};

export default RunDetail;