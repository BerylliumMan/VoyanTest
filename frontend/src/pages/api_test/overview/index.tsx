import React, { useCallback, useEffect, useState } from 'react';
import { Button, Card, Form, Grid, Input, Message, Progress, Select, Tag } from '@arco-design/web-react';
import {
  IconCheckCircleFill,
  IconCode,
  IconHistory,
  IconList,
  IconApps,
  IconExclamationCircleFill,
} from '@arco-design/web-react/icon';
import { apiGet, apiPost } from '@/utils/apiRequest';
import styles from '../../dashboard/style/index.module.less';

const { Row, Col } = Grid;
const FormItem = Form.Item;

interface Project {
  id: number;
  name: string;
}

interface CoverageItem {
  definition_id: number;
  name: string;
  method: string;
  path: string;
  referenced: boolean;
  reference_count: number;
  last_status: string | null;
  last_run_at: string | null;
}

interface CoverageGap {
  definition_id: number;
  path: string;
  reason: string;
}

interface Overview {
  definition_count: number;
  case_count: number;
  scenario_count: number;
  uncovered_definitions: number;
  coverage: number | null;
  scenario_runs_7d: number;
  scenario_pass_rate_7d: number | null;
  /** 031（US11）：双口径 */
  covered_by_last_run?: number;
  coverage_items?: CoverageItem[];
  coverage_gaps?: CoverageGap[];
}

const percent = (value: number | null) => (value == null ? '--' : `${Math.round(value * 100)}%`);

const StatCard: React.FC<{
  label: string;
  value: string | number;
  icon: React.ReactNode;
  tone?: 'primary' | 'success' | 'danger';
}> = ({ label, value, icon, tone = 'primary' }) => (
  <Card hoverable className={styles['stat-card']}>
    <div className={styles['stat-card-content']}>
      <div className={`${styles['stat-icon-container']} ${styles[`stat-icon-${tone}`]}`}>{icon}</div>
      <div className={styles['stat-text']}>
        <div className={styles['stat-label']}>{label}</div>
        <div className={styles['stat-value']}>{value}</div>
      </div>
    </div>
  </Card>
);

const ApiTestOverview: React.FC = () => {
  const [projects, setProjects] = useState<Project[]>([]);
  const [projectId, setProjectId] = useState<number | null>(null);
  const [data, setData] = useState<Overview | null>(null);
  const [strategy, setStrategy] = useState<any>(null);
  const [strategyLoading, setStrategyLoading] = useState(false);
  const [strategyProgress, setStrategyProgress] = useState(0);
  const [strategyMessage, setStrategyMessage] = useState('');
  const [strategyPid, setStrategyPid] = useState<string | null>(null);
  const [url, setUrl] = useState('');
  const [cron, setCron] = useState('0 2 * * *');
  const [mode, setMode] = useState('skip');
  const [basicUser, setBasicUser] = useState('');
  const [basicPassword, setBasicPassword] = useState('');
  const [saving, setSaving] = useState(false);

  const load = useCallback(async (id: number) => {
    const overview = await apiGet<Overview>('/api/api-test/overview', { project_id: id });
    setData(overview);
  }, []);

  const loadStrategy = useCallback(async () => {
    if (!projectId) return;
    const pid = (typeof crypto !== 'undefined' && (crypto as any).randomUUID)
      ? (crypto as any).randomUUID()
      : `strategy-${Date.now()}-${Math.random().toString(16).slice(2)}`;
    setStrategyPid(pid);
    setStrategyProgress(0);
    setStrategyMessage('正在提交分析任务…');
    setStrategyLoading(true);
    try {
      const res = await apiPost<any>('/api/api-test/ai/strategy', {
        project_id: projectId,
        progress_id: pid,
      });
      setStrategyProgress(100);
      setStrategyMessage('策略生成完成');
      setStrategy(res?.result || null);
    } catch (err: any) {
      Message.error(err?.response?.data?.detail || 'AI 策略生成失败');
    } finally {
      setStrategyLoading(false);
    }
  }, [projectId]);

  useEffect(() => {
    if (!strategyLoading || !strategyPid) return undefined;
    let stopped = false;
    const tick = async () => {
      try {
        const res = await apiGet<any>(`/api/api-test/ai/strategy/status/${strategyPid}`);
        if (stopped) return;
        setStrategyProgress(Math.max(0, Math.min(100, Number(res?.progress || 0))));
        if (res?.message) setStrategyMessage(String(res.message));
      } catch {
        // 任务刚启动时可能还没有进度，下一轮再取
      }
    };
    tick();
    const timer = window.setInterval(tick, 1000);
    return () => { stopped = true; window.clearInterval(timer); };
  }, [strategyLoading, strategyPid]);

  useEffect(() => {
    apiGet<Project[]>('/api/projects/')
      .then((list) => {
        const items = Array.isArray(list) ? list : [];
        setProjects(items);
        if (items[0]) setProjectId(items[0].id);
      })
      .catch(() => setProjects([]));
  }, []);

  useEffect(() => {
    if (projectId) load(projectId).catch(() => setData(null));
  }, [projectId, load]);

  return (
    <div>
      <div className={styles.header}>
        <Select
          placeholder="请选择项目"
          className={styles['select-project']}
          value={projectId ?? undefined}
          onChange={(v) => setProjectId(v as number)}
          options={projects.map((p) => ({ label: p.name, value: p.id }))}
          showSearch
        />
      </div>
      <Row gutter={16} className={styles['stats-row']}>
        <Col span={6}>
          <StatCard label="接口" value={data?.definition_count ?? 0} icon={<IconCode />} />
        </Col>
        <Col span={6}>
          <StatCard label="用例" value={data?.case_count ?? 0} icon={<IconList />} />
        </Col>
        <Col span={6}>
          <StatCard label="场景" value={data?.scenario_count ?? 0} icon={<IconApps />} />
        </Col>
        <Col span={6}>
          <StatCard
            label="接口覆盖率"
            value={percent(data?.coverage ?? null)}
            icon={<IconCheckCircleFill />}
            tone="success"
          />
        </Col>
      </Row>
      <Row gutter={16} className={styles['stats-row']}>
        <Col span={6}>
          <StatCard
            label="未挂用例的接口"
            value={data?.uncovered_definitions ?? 0}
            icon={<IconExclamationCircleFill />}
            tone="danger"
          />
        </Col>
        <Col span={6}>
          {/* 031（US11）：第二口径 —— 最近执行通过（引用了但从没通过不算真覆盖） */}
          <StatCard
            label="最近执行通过"
            value={data?.covered_by_last_run ?? 0}
            icon={<IconCheckCircleFill />}
            tone="success"
          />
        </Col>
        <Col span={6}>
          <StatCard label="近 7 天场景执行" value={data?.scenario_runs_7d ?? 0} icon={<IconHistory />} />
        </Col>
        <Col span={6}>
          <StatCard
            label="近 7 天步骤通过率"
            value={percent(data?.scenario_pass_rate_7d ?? null)}
            icon={<IconCheckCircleFill />}
            tone="success"
          />
        </Col>
      </Row>

      {/* 031（US11）：覆盖缺口清单（引用口径 + 执行口径的综合建议） */}
      {(data?.coverage_gaps?.length ?? 0) > 0 && (
        <Card title="覆盖缺口（建议优先补测）" className={styles['stat-card']} style={{ marginTop: 16 }}>
          {(data?.coverage_gaps || []).slice(0, 10).map((g) => (
            <div key={g.definition_id} style={{ padding: '6px 0', fontSize: 13, color: 'var(--vt-text-2)' }}>
              <code style={{ marginRight: 8 }}>{g.path || `#${g.definition_id}`}</code>
              <span style={{ color: 'var(--vt-text-3)' }}>{g.reason}</span>
            </div>
          ))}
          {(data?.coverage_gaps?.length ?? 0) > 10 && (
            <div style={{ color: 'var(--vt-text-3)', fontSize: 12 }}>
              仅显示前 10 条，共 {data?.coverage_gaps?.length} 条
            </div>
          )}
        </Card>
      )}
      <Card
        title="AI 测试策略"
        className={styles['stat-card']}
        style={{ marginTop: 16 }}
        extra={<Button size="small" loading={strategyLoading} onClick={loadStrategy}>生成建议</Button>}
      >
        {strategyLoading && (
          <div style={{ marginBottom: 10 }}>
            <Progress percent={strategyProgress} animation />
            <div style={{ color: 'var(--vt-text-3)', fontSize: 12 }}>{strategyMessage || 'AI 正在分析…'}</div>
          </div>
        )}
        {!strategy && !strategyLoading && <div style={{ color: 'var(--vt-text-3)', fontSize: 13 }}>按风险排序的补测建议（未覆盖接口 / 弱断言 / 近期失败）</div>}
        {strategy && (
          <>
            <div style={{ marginBottom: 8 }}>{strategy.summary}</div>
            {(strategy.items || []).map((item: any, idx: number) => (
              <div key={idx} style={{ padding: '6px 0', fontSize: 13 }}>
                <Tag color={item.priority === 'high' ? 'red' : item.priority === 'medium' ? 'orange' : 'gray'}>
                  {item.priority}
                </Tag>
                <span style={{ marginLeft: 8 }}>{item.reason}</span>
                <span style={{ marginLeft: 8, color: 'var(--vt-text-3)' }}>
                  {item.action}
                  {item.definition_ids?.length ? ` · 接口 ${item.definition_ids.join(',')}` : ''}
                  {item.case_ids?.length ? ` · 用例 ${item.case_ids.slice(0, 5).join(',')}` : ''}
                </span>
                {item.definition_ids?.length > 0 && (
                  <Button
                    size="mini"
                    type="outline"
                    style={{ marginLeft: 8 }}
                    onClick={() => {
                      window.location.href = `/api_test/definitions?project_id=${projectId}&generate=${item.definition_ids.join(',')}`;
                    }}
                  >
                    生成用例
                  </Button>
                )}
              </div>
            ))}
          </>
        )}
      </Card>
      <Card title="Swagger URL 定时同步">
        <Form layout="inline">
          <FormItem label="文档地址">
            <Input
              style={{ width: 420 }}
              value={url}
              onChange={setUrl}
              placeholder="https://example.com/openapi.json"
            />
          </FormItem>
          <FormItem label="Cron">
            <Input style={{ width: 140 }} value={cron} onChange={setCron} />
          </FormItem>
          <FormItem label="用户名">
            <Input style={{ width: 160 }} value={basicUser} onChange={setBasicUser} placeholder="可选" />
          </FormItem>
          <FormItem label="密码">
            <Input.Password style={{ width: 160 }} value={basicPassword} onChange={setBasicPassword} placeholder="可选" />
          </FormItem>
          <FormItem label="导入模式">
            <Select
              style={{ width: 140 }}
              value={mode}
              onChange={setMode}
              options={[
                { label: '跳过已有', value: 'skip' },
                { label: '覆盖已有', value: 'overwrite' },
              ]}
            />
          </FormItem>
          <FormItem>
            <Button
              type="primary"
              loading={saving}
              disabled={!projectId || !url}
              onClick={async () => {
                if (!projectId) return;
                setSaving(true);
                try {
                  await apiPost('/api/api-test/imports/schedule', {
                    project_id: projectId,
                    url,
                    cron,
                    mode,
                    basic_username: basicUser || null,
                    basic_password: basicPassword || null,
                  });
                  setBasicPassword('');
                  Message.success('已加入任务中心，将按 cron 执行');
                } finally {
                  setSaving(false);
                }
              }}
            >
              创建定时同步
            </Button>
          </FormItem>
        </Form>
      </Card>
    </div>
  );
};

export default ApiTestOverview;
