import React, { useEffect, useState } from 'react';
import { Card, Grid, Select, Spin, Table, Tag } from '@arco-design/web-react';
import { IconLoading, IconStorage, IconCheckCircleFill, IconCloseCircleFill, IconList } from '@arco-design/web-react/icon';
import axios from 'axios';
import useLocale from '@/utils/useLocale';
import logger from '@/utils/logger';
import styles from './style/index.module.less';

interface ProjectSummary { id: number; name: string; last_run_status?: string; }
interface DashboardStats { total_runs: number; passed: number; failed: number; pass_rate: number | null; }
interface TrendItem { date: string; label?: string; total: number; passed: number; failed: number; }
interface BatchRun { id: number; name: string; project_name: string; status: string; passed: number; total_cases: number; }

const { Row, Col } = Grid;

function Dashboard() {
  const t = useLocale();
  const [stats, setStats] = useState<DashboardStats>({} as DashboardStats);
  const [trends, setTrends] = useState<TrendItem[]>([]);
  const [recent, setRecent] = useState<BatchRun[]>([]);
  const [loading, setLoading] = useState(true);
  const [projects, setProjects] = useState<ProjectSummary[]>([]);
  const [projectId, setProjectId] = useState<number | undefined>(undefined);

  useEffect(() => {
    axios.get('/api/projects/').then((res) => setProjects(res.data || [])).catch(() => {});
  }, []);

  const fetchData = (pid?: number) => {
    setLoading(true);
    const params = pid ? { project_id: pid } : {};
    Promise.all([
      axios.get('/api/reports/statistics', { params }),
      axios.get('/api/reports/trends', { params: { ...params, days: 7 } }),
      axios.get('/api/reports/batches', { params: { ...params, page: 1, size: 5 } }),
    ]).then(([statsRes, trendsRes, batchesRes]) => {
      setStats(statsRes.data);
      setTrends(trendsRes.data.data || []);
      setRecent(batchesRes.data.items || []);
    }).catch((err) => { logger.error('Failed to load dashboard data:', err); }).finally(() => setLoading(false));
  };

  useEffect(() => { fetchData(projectId); }, [projectId]);

  if (loading) return <Spin loading className={styles['loading-spin']} />;

  return (
    <div>
      <div className={styles.header}>
        <Select
          placeholder={t['select.project']} className={styles['select-project']} allowClear
          value={projectId}
          onChange={(val) => setProjectId(val)}
          options={projects.map((p: ProjectSummary) => ({ label: p.name, value: p.id }))}
          showSearch
        />
      </div>
      <Row gutter={16} className={styles['stats-row']}>
        <Col span={6}>
          <Card hoverable className={styles['stat-card']}>
            <div className={styles['stat-card-content']}>
              <div className={`${styles['stat-icon-container']} ${styles['stat-icon-primary']}`}>
                <IconStorage />
              </div>
              <div className={styles['stat-text']}>
                <div className={styles['stat-label']}>{t['total.runs']}</div>
                <div className={styles['stat-value']}>{stats.total_runs || 0}</div>
              </div>
            </div>
          </Card>
        </Col>
        <Col span={6}>
          <Card hoverable className={styles['stat-card']}>
            <div className={styles['stat-card-content']}>
              <div className={`${styles['stat-icon-container']} ${styles['stat-icon-success']}`}>
                <IconCheckCircleFill />
              </div>
              <div className={styles['stat-text']}>
                <div className={styles['stat-label']}>{t['passed']}</div>
                <div className={styles['stat-value']}>{stats.passed || 0}</div>
              </div>
            </div>
          </Card>
        </Col>
        <Col span={6}>
          <Card hoverable className={styles['stat-card']}>
            <div className={styles['stat-card-content']}>
              <div className={`${styles['stat-icon-container']} ${styles['stat-icon-danger']}`}>
                <IconCloseCircleFill />
              </div>
              <div className={styles['stat-text']}>
                <div className={styles['stat-label']}>{t['failed']}</div>
                <div className={styles['stat-value']}>{stats.failed || 0}</div>
              </div>
            </div>
          </Card>
        </Col>
        <Col span={6}>
          <Card hoverable className={styles['stat-card']}>
            <div className={styles['stat-card-content']}>
              <div className={`${styles['stat-icon-container']} ${styles['stat-icon-primary']}`}>
                <IconList />
              </div>
              <div className={styles['stat-text']}>
                <div className={styles['stat-label']}>{t['pass.rate']}</div>
                <div className={styles['stat-value']}>
                  {stats.pass_rate != null ? `${stats.pass_rate.toFixed(1)}%` : '--'}
                </div>
              </div>
            </div>
          </Card>
        </Col>
      </Row>

      <Row gutter={16} className={styles.rowFlex}>
        <Col span={12} className={styles.colFlex}>
          <Card title={t['trend.7days']} className={styles.flexCard}>
            {trends.length ? (
              <div className={styles['trend-bar']}>
                {trends.map((d: TrendItem, i: number) => {
                  const maxVal = Math.max(...trends.map((t: TrendItem) => t.total || 0), 1);
                  const h = ((d.total || 0) / maxVal) * 120;
                  return (
                    <div key={i} className={styles['trend-bar-col']}>
                      <div className={styles['trend-bar-count']}>{d.total}</div>
                      <div className={styles['trend-bar-fill']} style={{ height: h }} />
                      <div className={styles['trend-bar-label']}>{d.label || d.date?.slice(5) || ''}</div>
                    </div>
                  );
                })}
              </div>
            ) : <div className={styles['empty-state']}>{t['no.data']}</div>}
          </Card>
        </Col>
        <Col span={12} className={styles.colFlex}>
          <Card title={t['recent.runs']} className={styles.flexCard}>
            {recent.length ? (
              <Table
                data={recent} rowKey="id" pagination={false} size="small"
                columns={[
                  { title: t['batch'], dataIndex: 'name', ellipsis: true },
                  { title: t['project'], dataIndex: 'project_name', width: 100, ellipsis: true },
                  { title: t['status'], dataIndex: 'status', width: 100,
 render: (_: unknown, r: BatchRun) => {
                       const s = r.status;
                       if (s === 'running') return <Tag color="blue"><IconLoading className={styles.iconMarginRight} />{t['running']}</Tag>;
                       if (r.passed === r.total_cases && r.total_cases > 0) return <Tag color="green">{t['all.passed']}</Tag>;
                       if (r.passed > 0) return <Tag color="orange">{t['partial.passed']}</Tag>;
                       if (r.total_cases > 0) return <Tag color="red">{t['all.failed']}</Tag>;
                       return <Tag color="gray">--</Tag>;
                     } },
                   { title: t['passed.total'], width: 100,
                     render: (_: unknown, r: BatchRun) => `${r.passed || 0}/${r.total_cases || 0}` },
                ]}
              />
            ) : <div className={styles['empty-state']}>{t['no.data']}</div>}
          </Card>
        </Col>
      </Row>
    </div>
  );
}

export default Dashboard;
