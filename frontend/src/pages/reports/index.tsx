import React, { useEffect, useState, useCallback, useRef } from 'react';
import {
  Card, Table, Tag, Spin, Button, Modal, Descriptions, Message, Space, Select,
} from '@arco-design/web-react';
import { IconEye, IconDown, IconRight, IconLoading, IconDownload, IconDelete } from '@arco-design/web-react/icon';
import axios from 'axios';
import { apiRequest } from '@/utils/apiRequest';
import useLocale from '@/utils/useLocale';
import RunDetail from './RunDetail';
import styles from './style/index.module.less';

interface BatchItem {
  id: number; name: string; project_id: number; project_name: string;
  status: string; total_cases: number; passed: number; failed: number;
  created_at: string; started_at: string; finished_at: string;
  triggered_by?: string;
}

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
  id: number; run_id: number; case_id: number; case_name: string;
  status: string; duration: number;
  started_at: string; finished_at: string;
  steps: StepDetail[];
  logs?: string;
}

interface BatchDetail {
  id: number; name: string; project_id: number; project_name: string;
  status: string; total_cases: number; passed: number; failed: number;
  created_at: string; started_at: string; finished_at: string;
  runs: RunItem[];
}

const STATUS_COLORS: Record<string, string> = {
  pending: 'blue', running: 'blue', passed: 'green', failed: 'red', skipped: 'orange',
};

const Reports: React.FC = () => {
  const t = useLocale();

  const STATUS_LABELS: Record<string, string> = {
    pending: t['step.waiting'],
    running: t['running'],
    passed: t['passed'],
    failed: t['failed'],
    skipped: 'Skipped',
  };

  const getStatusTag = (s: string, animated?: boolean) => (
    <Tag color={STATUS_COLORS[s] || 'blue'}>
      {animated ? <><IconLoading className={styles.iconMarginRight} />{STATUS_LABELS[s] || s}</> : STATUS_LABELS[s] || s}
    </Tag>
  );

  const getBatchStatusTag = (s: string, passed: number, total: number) => {
    if (s === 'running') return <Tag color="blue"><IconLoading className={styles.iconMarginRight} />{t['running']}</Tag>;
    if (passed === total) return <Tag color="green">{t['all.passed']}</Tag>;
    if (passed > 0) return <Tag color="orange">{t['partial.passed']}</Tag>;
    if (total > 0) return <Tag color="red">{t['all.failed']}</Tag>;
    return <Tag color="gray">--</Tag>;
  };

  const [data, setData] = useState<BatchItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);
  const [total, setTotal] = useState(0);
  const [detail, setDetail] = useState<BatchDetail | null>(null);
  const [detailVisible, setDetailVisible] = useState(false);
  const [detailLoading, setDetailLoading] = useState(false);
  const [selectedRun, setSelectedRun] = useState<RunItem | null>(null);
  const [runVisible, setRunVisible] = useState(false);
  const [expandedRuns, setExpandedRuns] = useState<Set<number>>(new Set());
  const [pollingBatchId, setPollingBatchId] = useState<number | null>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const [compareVisible, setCompareVisible] = useState(false);
  const [compareA, setCompareA] = useState<number | undefined>();
  const [compareB, setCompareB] = useState<number | undefined>();
  const [compareResult, setCompareResult] = useState<any>(null);

  const getCookie = (name: string) => {
    const match = document.cookie.match(new RegExp('(^| )' + name + '=([^;]+)'));
    return match ? match[2] : null;
  };

  const fetchData = useCallback(async () => {
    setLoading(true);
    try {
      const data = await apiRequest<{ items?: BatchItem[]; total?: number }>(
        { method: 'GET', url: '/api/reports/batches', params: { page, size: pageSize } },
        { showSuccess: false, showError: false }
      );
      setData(data.items || []);
      setTotal(data.total || 0);
    } catch {
      setData([]);
      setTotal(0);
    } finally {
      setLoading(false);
    }
  }, [page, pageSize]);

  useEffect(() => {
    fetchData();
  }, [fetchData]);

  const refreshBatchDetail = async (batchId: number) => {
    try {
      const updated = await apiRequest<BatchDetail>(
        { method: 'GET', url: `/api/reports/batches/${batchId}` },
        { showSuccess: false, showError: false }
      );
      setDetail(updated);
      if (updated.status !== 'running') {
        setPollingBatchId(null);
      }
    } catch {
      setPollingBatchId(null);
    }
  };

  useEffect(() => {
    if (pollingBatchId !== null) {
      const sessionId = getCookie('session_id');
      if (!sessionId) {
        setPollingBatchId(null);
        return;
      }

      const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
      // 同源 WebSocket 连接自动携带 cookie，不再通过 URL 传递 session_id
      const wsUrl = `${protocol}//${window.location.host}/ws/logs/${pollingBatchId}`;
      const ws = new WebSocket(wsUrl);
      wsRef.current = ws;
      let wsActive = true;

      ws.onopen = () => {
        wsActive = true;
      };

      ws.onmessage = (event) => {
        try {
          const msg = JSON.parse(event.data);
          if (msg.type === 'run_complete' || msg.type === 'step_complete') {
            refreshBatchDetail(pollingBatchId);
          }
        } catch { /* ignore parse errors */ }
      };

      ws.onclose = () => {
        wsActive = false;
        wsRef.current = null;
        if (pollingBatchId !== null) {
          pollRef.current = setInterval(() => {
            refreshBatchDetail(pollingBatchId);
          }, 3000);
        }
      };

      ws.onerror = () => {
        wsActive = false;
        wsRef.current = null;
        if (pollingBatchId !== null) {
          pollRef.current = setInterval(() => {
            refreshBatchDetail(pollingBatchId);
          }, 3000);
        }
      };
    }
    return () => {
      if (wsRef.current) {
        wsRef.current.close();
        wsRef.current = null;
      }
      if (pollRef.current) {
        clearInterval(pollRef.current);
        pollRef.current = null;
      }
    };
  }, [pollingBatchId]);

  const viewBatch = async (batchId: number) => {
    setDetailLoading(true);
    setDetailVisible(true);
    setDetail(null);
    try {
      const res = await axios.get(`/api/reports/batches/${batchId}`);
      setDetail(res.data);
      if (res.data.status === 'running') {
        setPollingBatchId(batchId);
      }
    } catch {
      setDetail(null);
    } finally {
      setDetailLoading(false);
    }
  };

  const handleCloseDetail = () => {
    setDetailVisible(false);
    setPollingBatchId(null);
    setDetail(null);
  };

  const viewRun = (run: RunItem) => {
    setSelectedRun(run);
    setRunVisible(true);
  };

  const toggleRun = (runId: number) => {
    setExpandedRuns((prev) => {
      const next = new Set(prev);
      if (next.has(runId)) next.delete(runId);
      else next.add(runId);
      return next;
    });
  };

  const handleExport = async (batchId: number, batchName: string) => {
    try {
      const res = await axios.get(`/api/reports/batches/${batchId}`);
      const blob = new Blob([JSON.stringify(res.data, null, 2)], { type: 'application/json' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `report_${batchName || batchId}.json`;
      a.click();
      URL.revokeObjectURL(url);
    } catch { Message.error(t['export.failed']); }
  };

  const handleDelete = (batchId: number, batchName: string) => {
    Modal.confirm({
      title: t['confirm.delete.item'],
      content: `${t['delete']} "${batchName}"?`,
      okText: t['delete'],
      cancelText: t['cancel'],
      okButtonProps: { status: 'danger' },
      onOk: async () => {
        try {
          await axios.delete(`/api/reports/batches/${batchId}`);
          Message.success(t['deleted']);
          fetchData();
        } catch { Message.error(t['delete.failed']); }
      },
    });
  };

  const columns = [
    { title: t['batch'], dataIndex: 'name', width: 200, ellipsis: true },
    { title: t['project'], dataIndex: 'project_name', width: 150, ellipsis: true },
    {
      title: t['status'], width: 120,
      render: (_: unknown, r: BatchItem) => getBatchStatusTag(r.status, r.passed, r.total_cases),
    },
    {
      title: t['result'], width: 180,
      render: (_: unknown, r: BatchItem) => (
        <span>
          <Tag color="green">{t['passed'] + ' ' + r.passed}</Tag>
          <Tag color="red" className={styles.tagMarginLeft}>{t['failed'] + ' ' + r.failed}</Tag>
          <span className={styles.resultMeta}>{`/ ${r.total_cases} ${t['case.count']}`}</span>
        </span>
      ),
    },
    { title: t['exec.time'], dataIndex: 'created_at', width: 180,
      render: (v: string) => v ? new Date(v).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai' }) : '--' },
    { title: t['executor'], dataIndex: 'triggered_by', width: 120, ellipsis: true,
      render: (v: string | undefined) => v || '--' },
    {
      title: t['actions'], width: 280,
      render: (_: unknown, r: BatchItem) => (
        <Space>
          <Button type="primary" size="small" icon={<IconEye />} onClick={() => viewBatch(r.id)}>
            {t['detail']}
          </Button>
          <Button size="small" icon={<IconDownload />} onClick={() => handleExport(r.id, r.name)}>
            {t['export']}
          </Button>
          <Button size="small" status="danger" icon={<IconDelete />} onClick={() => handleDelete(r.id, r.name)}>
            {t['delete']}
          </Button>
        </Space>
      ),
    },
  ];

  return (
    <div>
      <Card className={styles['card-full']}>
        <div style={{ display: 'flex', justifyContent: 'flex-end', marginBottom: 12 }}>
          <Button onClick={() => { setCompareVisible(true); setCompareResult(null); }}>对比批次</Button>
        </div>
        <Spin loading={loading} className={styles['spin-full']}>
          <Table
            columns={columns} data={data} rowKey="id" stripe
            scroll={{ x: 900 }}
            pagination={{
              total, current: page, pageSize,
              onChange: (p, ps) => { setPage(p); setPageSize(ps); },
              showTotal: true,
              sizeOptions: [20, 50, 100],
              sizeCanChange: true,
            }}
          />
        </Spin>
      </Card>

      {/* Batch Detail Modal */}
      <Modal
        visible={detailVisible} onCancel={handleCloseDetail}
        title={
          detail
            ? t['batch.detail'].replace('{name}', String(detail.name || detail.id))
            : t['loading']
        }
        footer={<Button onClick={handleCloseDetail}>{t['close']}</Button>}
        className={styles.modalWide}
      >
        <Spin loading={detailLoading}>
          {detail && (
            <div>
              <Descriptions
                column={3}
                data={[
                  { label: t['project'], value: detail.project_name },
                  { label: t['status'], value: getStatusTag(detail.status, detail.status === 'running') },
                  { label: t['case.count'], value: detail.total_cases },
                  { label: t['passed'], value: detail.passed },
                  { label: t['failed'], value: detail.failed },
                  { label: t['start.time'], value: detail.started_at ? new Date(detail.started_at).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai' }) : '--' },
                ]}
                className={styles.descriptionsMargin}
              />

              {detail.runs && detail.runs.length > 0 ? (
                <div>
                  <h4 className={styles['run-section-title']}>{t['case.steps']}</h4>
                  {detail.runs.map((run) => {
                    const expanded = expandedRuns.has(run.run_id);
                    const isRunning = run.status === 'running';
                    return (
                    <Card key={run.run_id} className={`${styles['batch-card']} ${styles.runCardBody}`} style={{
                      borderLeft: `3px solid ${
                        run.status === 'passed' ? 'var(--color-success-6)' :
                        run.status === 'failed' ? 'var(--color-danger-6)' :
                        run.status === 'running' ? 'var(--color-primary-6)' :
                        'var(--color-border-2)'
                      }`,
                    }}
                    >
                      <div className={styles['batch-header']}
                        onClick={() => toggleRun(run.run_id)}
                        role="button" tabIndex={0}
                        onKeyDown={(e) => e.key === 'Enter' && toggleRun(run.run_id)}
                      >
                        {expanded ? <IconDown /> : <IconRight />}
                        <span className={styles['batch-name']}>{run.case_name}</span>
                        {getStatusTag(run.status, isRunning)}
                        <span className={styles['batch-time']}>
                          {run.status === 'running' ? t['step.executing'] : run.duration ? `${run.duration.toFixed(1)}s` : ''}
                          {!isRunning && run.steps?.length ? ` · ${t['steps'].replace('{count}', String(run.steps.length))}` : ''}
                        </span>
                      </div>
                      {expanded && run.steps && run.steps.length > 0 && (
                        <div className={styles.stepTopMargin}>
                          {run.steps.map((step: StepDetail, idx: number) => (
                            <Card key={idx} className={`${styles['step-card']}${step.success === undefined && isRunning ? ` ${styles['step-dimmed']}` : ''} ${styles.stepCardBody}`}
                            >
                              <div className={styles['step-header']}>
                                <Tag className={styles.stepIndexTag}>
                                  {step.step_number || idx + 1}
                                </Tag>
                                <span className={styles['step-description']}>
                                  {step.original_description || step.description}
                                </span>
                                {step.status
                                  ? getStatusTag(step.status === 'skipped' ? 'skipped' : step.success ? 'passed' : 'failed')
                                  : step.success !== undefined
                                    ? getStatusTag(step.success ? 'passed' : 'failed')
                                    : isRunning
                                      ? <Tag color="blue"><IconLoading className={styles.iconMarginRightSmall} />{t['step.waiting']}</Tag>
                                      : getStatusTag('failed')
                                }
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
                                <div className={styles.screenshotWrapper}>
                                  <img
                                    src={`/${step.screenshot_path}`}
                                    alt={t['screenshot'].replace('{num}', String(step.step_number ?? ''))}
                                    className={styles['step-screenshot']}
                                  />
                                </div>
                              )}
                            </Card>
                          ))}
                        </div>
                      )}
                      {expanded && (!run.steps || run.steps.length === 0) && (
                        <div className={styles.stepEmptyText}>
                          {isRunning ? t['step.executing'] : t['no.steps']}
                        </div>
                      )}
                    </Card>
                    );
                  })}
                </div>
              ) : (
                <div className={styles['no-data']}>{t['no.runs']}</div>
              )}
            </div>
          )}
        </Spin>
      </Modal>

      {/* Run Detail Modal */}
      <RunDetail visible={runVisible} run={selectedRun} onClose={() => setRunVisible(false)} />

      {/* Compare Modal */}
      <Modal title="报告对比" visible={compareVisible} onCancel={() => setCompareVisible(false)} footer={null}>
        <Space style={{ marginBottom: 16 }}>
          <Select placeholder="批次 A" style={{ width: 200 }} value={compareA} onChange={setCompareA}>
            {data.map((b: any) => <Select.Option key={b.id} value={b.id}>{b.name}</Select.Option>)}
          </Select>
          <Select placeholder="批次 B" style={{ width: 200 }} value={compareB} onChange={setCompareB}>
            {data.map((b: any) => <Select.Option key={b.id} value={b.id}>{b.name}</Select.Option>)}
          </Select>
          <Button type="primary" onClick={async () => {
            if (!compareA || !compareB) return;
            try {
              const r = await axios.post(`/api/reports/compare?batch_a=${compareA}&batch_b=${compareB}`);
              setCompareResult(r.data);
            } catch { Message.error('对比失败'); }
          }}>对比</Button>
        </Space>
        {compareResult && (
          <Descriptions column={2} data={[
            { label: '批次 A', value: `${compareResult.a.name} (${compareResult.a.status})` },
            { label: '批次 B', value: `${compareResult.b.name} (${compareResult.b.status})` },
            { label: '通过 diff', value: <span style={{ color: (compareResult.passed_diff || 0) >= 0 ? 'green' : 'red' }}>{compareResult.passed_diff}</span> },
            { label: '失败 diff', value: <span style={{ color: (compareResult.failed_diff || 0) <= 0 ? 'green' : 'red' }}>{compareResult.failed_diff}</span> },
          ]} />
        )}
      </Modal>
    </div>
  );
};

export default Reports;
