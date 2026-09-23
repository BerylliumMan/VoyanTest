import React, { useCallback, useEffect, useState } from 'react';
import { Button, Card, Form, Grid, Input, Message, Select } from '@arco-design/web-react';
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

interface Overview {
  definition_count: number;
  case_count: number;
  scenario_count: number;
  uncovered_definitions: number;
  coverage: number | null;
  scenario_runs_7d: number;
  scenario_pass_rate_7d: number | null;
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
        <Col span={8}>
          <StatCard
            label="未挂用例的接口"
            value={data?.uncovered_definitions ?? 0}
            icon={<IconExclamationCircleFill />}
            tone="danger"
          />
        </Col>
        <Col span={8}>
          <StatCard label="近 7 天场景执行" value={data?.scenario_runs_7d ?? 0} icon={<IconHistory />} />
        </Col>
        <Col span={8}>
          <StatCard
            label="近 7 天步骤通过率"
            value={percent(data?.scenario_pass_rate_7d ?? null)}
            icon={<IconCheckCircleFill />}
            tone="success"
          />
        </Col>
      </Row>
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
