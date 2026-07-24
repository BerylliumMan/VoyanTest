import React, { useState, useEffect, useCallback } from 'react';
import {
  Card,
  Table,
  Button,
  Select,
  Message,
  Spin,
  Typography,
} from '@arco-design/web-react';
import { IconEye } from '@arco-design/web-react/icon';
import { useHistory } from 'react-router-dom';
import useLocale from '@/utils/useLocale';
import { apiRequest } from '@/utils/apiRequest';
import RunStatusBadge from './components/RunStatusBadge';
import { AgentRun, AgentRunStatus } from './types';
import styles from './style/index.module.less';

const { Title } = Typography;

/** 状态筛选项 */
const STATUS_OPTIONS: { label: string; value: string }[] = [
  { label: '', value: 'all' },
  { label: '', value: 'pending' },
  { label: '', value: 'running' },
  { label: '', value: 'completed' },
  { label: '', value: 'failed' },
  { label: '', value: 'cancelled' },
];

const AgentRunsList: React.FC = () => {
  const t = useLocale();
  const history = useHistory();

  const [runs, setRuns] = useState<AgentRun[]>([]);
  const [loading, setLoading] = useState(false);
  const [pagination, setPagination] = useState({ current: 1, pageSize: 20, total: 0 });
  const [filterStatus, setFilterStatus] = useState<string>('all');

  /** 获取列表 */
  const fetchRuns = useCallback(async (page?: number, status?: string) => {
    const currentPage = page ?? pagination.current;
    const currentStatus = status ?? filterStatus;
    setLoading(true);
    try {
      const params: Record<string, string> = {
        page: String(currentPage),
        size: String(pagination.pageSize),
      };
      if (currentStatus && currentStatus !== 'all') {
        params.status = currentStatus;
      }
      const query = new URLSearchParams(params).toString();
      const data = await apiRequest(`/api/agent-runs?${query}`);
      setRuns(data.items || []);
      setPagination((prev) => ({
        ...prev,
        current: currentPage,
        total: data.total || 0,
      }));
    } catch {
      Message.error(t['agent_runs.list.load_failed'] || '加载运行列表失败');
    } finally {
      setLoading(false);
    }
  }, [pagination.current, pagination.pageSize, filterStatus, t]);

  useEffect(() => {
    fetchRuns(1, filterStatus);
  }, [filterStatus]);

  /** 翻页 */
  const handlePageChange = (page: number) => {
    setPagination((prev) => ({ ...prev, current: page }));
    fetchRuns(page);
  };

  /** 查看详情 */
  const handleViewDetail = (runId: number) => {
    history.push(`/agent-runs/${runId}`);
  };

  /** 格式化时间 */
  const formatTime = (ts: string | null): string => {
    if (!ts) return '-';
    return new Date(ts).toLocaleString();
  };

  /** 格式化耗时（ms 转显示） */
  const formatDuration = (ms: number | null): string => {
    if (ms === null || ms === undefined) return '-';
    const seconds = Math.floor(ms / 1000);
    if (seconds < 60) return `${seconds}s`;
    const min = Math.floor(seconds / 60);
    const sec = seconds % 60;
    return `${min}m ${sec}s`;
  };

  const columns = [
    {
      title: 'ID',
      dataIndex: 'id',
      width: 80,
    },
    {
      title: t['agent_runs.col.agent'] || 'Agent',
      dataIndex: 'agent_definition_id',
      width: 100,
      render: (_: unknown, record: AgentRun) =>
        record.agent_definition_name || `#${record.agent_definition_id}`,
    },
    {
      title: t['status'] || '状态',
      dataIndex: 'status',
      width: 100,
      render: (_: unknown, record: AgentRun) => (
        <RunStatusBadge status={record.status} />
      ),
    },
    {
      title: t['agent_runs.col.goal'] || '目标',
      dataIndex: 'goal',
      ellipsis: true,
      render: (_: unknown, record: AgentRun) => (
        <span title={typeof record.goal === 'string' ? record.goal : JSON.stringify(record.goal)}>
          {typeof record.goal === 'string' ? (record.goal.length > 60 ? `${record.goal.slice(0, 60)}...` : record.goal || '-')
          : record.goal ? JSON.stringify(record.goal).slice(0, 60) + '...' : '-'}
        </span>
      ),
    },
    {
      title: t['agent_runs.col.turns'] || '轮次',
      dataIndex: 'turns_used',
      width: 80,
    },
    {
      title: t['agent_runs.col.started'] || '开始时间',
      dataIndex: 'started_at',
      width: 170,
      render: (_: unknown, record: AgentRun) => formatTime(record.started_at),
    },
    {
      title: t['agent_runs.col.duration'] || '耗时',
      dataIndex: 'duration_ms',
      width: 100,
      render: (_: unknown, record: AgentRun) => formatDuration(record.duration_ms),
    },
    {
      title: t['actions'] || '操作',
      dataIndex: 'actions',
      width: 100,
      render: (_: unknown, record: AgentRun) => (
        <Button
          type="text"
          size="small"
          icon={<IconEye />}
          onClick={() => handleViewDetail(record.id)}
        >
          {t['detail'] || '详情'}
        </Button>
      ),
    },
  ];

  return (
    <div className={styles['agent-runs-page']}>
      <Card>
        <Title heading={5} style={{ marginTop: 0 }}>
          {t['agent_runs.list.title'] || 'Agent 运行记录'}
        </Title>

        {/* 工具栏 */}
        <div className={styles['agent-runs-toolbar']}>
          <div className="toolbar-left">
            <Select
              className={styles['status-filter']}
              placeholder={t['agent_runs.filter.status'] || '筛选状态'}
              value={filterStatus}
              onChange={(val: string) => setFilterStatus(val)}
              options={STATUS_OPTIONS.map((opt) => ({
                ...opt,
                label: opt.value === 'all'
                  ? (t['agent_runs.filter.all'] || '全部')
                  : (t[`agent_runs.status.${opt.value}`] || opt.value),
              }))}
              allowClear
            />
          </div>
          <Button
            type="outline"
            size="small"
            onClick={() => fetchRuns(1)}
            loading={loading}
          >
            {t['recordings.refresh'] || '刷新'}
          </Button>
        </div>

        {/* 表格 */}
        <Spin loading={loading} tip={t['loading'] || '加载中...'}>
          <Table
            columns={columns}
            data={runs}
            rowKey="id"
            pagination={{
              current: pagination.current,
              pageSize: pagination.pageSize,
              total: pagination.total,
              showTotal: true,
              onChange: handlePageChange,
            }}
            scroll={{ x: 1000 }}
          />
        </Spin>
      </Card>
    </div>
  );
};

export default AgentRunsList;
