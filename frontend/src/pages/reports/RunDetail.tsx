import React, { useEffect, useState } from 'react';
import { Modal, Descriptions, Card, Tag, Button, Message } from '@arco-design/web-react';
import axios from 'axios';
import useLocale from '@/utils/useLocale';
import { renderEvidenceLinks } from '@/utils/evidenceLinks';
import ApiStepDetail from './ApiStepDetail';
import styles from './style/index.module.less';
import type { StepDetail, RunItem } from './types';

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
  info: 'gray',
};

const RunDetail: React.FC<RunDetailProps> = ({ visible, run, onClose }) => {
  const t = useLocale();
  const [analysis, setAnalysis] = useState<any>(null);
  const [analyzing, setAnalyzing] = useState(false);
  const [contract, setContract] = useState<any>(null);
  const [checkingContract, setCheckingContract] = useState(false);

  useEffect(() => {
    setAnalysis(null);
    setContract(null);
  }, [run?.run_id, visible]);

  const analyzeFailure = async () => {
    if (!run?.run_id) return;
    setAnalyzing(true);
    try {
      const resp = await axios.post('/api/api-test/ai/analyze-failure', { run_id: run.run_id });
      setAnalysis(resp.data);
    } catch (err: any) {
      Message.error(err?.response?.data?.detail || 'AI 分析失败');
    } finally {
      setAnalyzing(false);
    }
  };

  const checkContract = async () => {
    if (!run?.run_id) return;
    setCheckingContract(true);
    try {
      const resp = await axios.post('/api/api-test/ai/check-contract', { run_id: run.run_id });
      setContract(resp.data);
    } catch (err: any) {
      Message.error(err?.response?.data?.detail || '契约检测失败');
    } finally {
      setCheckingContract(false);
    }
  };

  const STATUS_LABELS: Record<string, string> = {
    pending: t['step.waiting'],
    running: t['running'],
    passed: t['passed'],
    failed: t['failed'],
    skipped: t['skipped'],
    info: t['info'],
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
      className={styles.runDetailModal}
    >
      {run && (
        <div>
          <Descriptions
            column={2}
            data={[
              { label: t['status'], value: getStatusTag(run.status) },
              { label: t['duration'], value: run.duration ? `${run.duration.toFixed(1)}s` : '--' },
              { label: t['start.time'], value: run.started_at ? new Date(run.started_at).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai' }) : '--' },
              { label: t['end.time'], value: run.finished_at ? new Date(run.finished_at).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai' }) : '--' },
            ]}
            className={styles.descriptionsMarginLarge}
          />
          {run.status === 'failed' && (
            <div style={{ margin: '12px 0' }}>
              <Button type="primary" loading={analyzing} onClick={analyzeFailure}>
                AI 分析失败原因
              </Button>
            </div>
          )}
          {run.steps?.some((s) => s.request) && (
            <div style={{ margin: '12px 0' }}>
              <Button loading={checkingContract} onClick={checkContract}>契约检测</Button>
            </div>
          )}
          {contract && (
            <Card
              title="契约检测"
              style={{ marginBottom: 16 }}
              extra={<Tag color={contract.result?.breaking ? 'red' : 'green'}>
                {contract.result?.breaking ? '破坏性' : '兼容'}
              </Tag>}
            >
              <div style={{ marginBottom: 8 }}>{contract.result?.summary}</div>
              {(contract.result?.findings || []).length > 0 && (
                <ul style={{ margin: '4px 0 0 18px' }}>
                  {(contract.result.findings || []).map((f: any, i: number) => (
                    <li key={i}>{f.path}：期望 {String(f.expected)}，实际 {String(f.actual)}（{f.kind}）</li>
                  ))}
                </ul>
              )}
              {contract.result?.explanation && (
                <div style={{ marginTop: 8, color: 'var(--color-text-2)' }}>{contract.result.explanation}</div>
              )}
            </Card>
          )}
          {analysis && (
            <Card
              title="AI 失败归因"
              style={{ marginBottom: 16 }}
              extra={<Tag color={analysis.stale ? 'orange' : 'green'}>{analysis.stale ? '可能过期' : analysis.status}</Tag>}
            >
              <div style={{ marginBottom: 8 }}>
                <Tag color="arcoblue">{analysis.result?.root_cause}</Tag>
                <span style={{ marginLeft: 8 }}>{analysis.result?.summary}</span>
              </div>
              {(analysis.result?.evidence || []).length > 0 && (
                <div style={{ marginBottom: 8 }}>
                  <strong>证据：</strong>
                  <ul style={{ margin: '4px 0 0 18px' }}>
                    {(analysis.result.evidence || []).map((e: any, i: number) => (
                      <li key={i}>{e.ref ? `${e.ref}：` : ''}{e.detail}</li>
                    ))}
                  </ul>
                </div>
              )}
              {(analysis.result?.suggestions || []).length > 0 && (
                <div>
                  <strong>建议：</strong>
                  <ul style={{ margin: '4px 0 0 18px' }}>
                    {(analysis.result.suggestions || []).map((s: any, i: number) => (
                      <li key={i}>{s.action}{s.target ? `（${s.target}）` : ''}：{s.detail}</li>
                    ))}
                  </ul>
                </div>
              )}
            </Card>
          )}
          {run.steps && run.steps.length > 0 ? (
            <div>
              <h4 className={styles['run-section-title']}>{t['step.detail']}</h4>
              {run.steps.map((step: StepDetail, idx: number) => (
                <Card key={idx} className={`${styles['step-card']} ${styles.runDetailStepBody}`}
                >
                  <div className={styles['step-header']}>
                    <Tag className={styles.stepIndexTag}>
                      {step.step_number || idx + 1}
                    </Tag>
                    <span className={styles['step-description']}>{renderEvidenceLinks(step.original_description || step.description)}</span>
                    {step.status && getStatusTag(
                      step.status === 'skipped' ? 'skipped' :
                      step.status === 'info' ? 'info' :
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
                    <div className={styles.screenshotTopMargin}>
                      <img
                        src={`/${step.screenshot_path}`}
                        alt={t['screenshot'].replace('{num}', String(step.step_number ?? ''))}
                        className={styles['step-screenshot']}
                      />
                    </div>
                  )}
                  <ApiStepDetail step={step} />
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