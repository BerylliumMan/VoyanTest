import React, { useCallback, useEffect, useState } from 'react';
import {
  Button, Card, Empty, Space, Table, Tabs, Tag,
} from '@arco-design/web-react';
import { useHistory } from 'react-router-dom';
import { apiGet } from '@/utils/apiRequest';

interface MyTask {
  plan_item_id: number;
  plan_id: number;
  plan_name: string;
  project_id: number;
  project_name: string;
  case_id: number | null;
  case_name: string;
  status: string;
  priority: string;
  due_date?: string | null;
  overdue: boolean;
  assignee_id?: number | null;
  assignee_name?: string | null;
}

const STATUS: Record<string, { label: string; color: string }> = {
  not_started: { label: '未开始', color: 'gray' },
  in_progress: { label: '进行中', color: 'blue' },
  passed: { label: '通过', color: 'green' },
  failed: { label: '失败', color: 'red' },
  blocked: { label: '阻塞', color: 'orange' },
};

const PRIORITY: Record<string, { label: string; color: string }> = {
  low: { label: '低', color: 'gray' },
  medium: { label: '中', color: 'blue' },
  high: { label: '高', color: 'orange' },
  urgent: { label: '紧急', color: 'red' },
};

const MyTasks: React.FC = () => {
  const history = useHistory();
  const [filter, setFilter] = useState('todo');
  const [items, setItems] = useState<MyTask[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [size, setSize] = useState(20);
  const [loading, setLoading] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const res = await apiGet<{ items: MyTask[]; total: number }>('/api/my/tasks', {
        filter, page, size,
      });
      setItems(res.items || []);
      setTotal(res.total || 0);
    } finally {
      setLoading(false);
    }
  }, [filter, page, size]);

  useEffect(() => { load(); }, [load]);

  useEffect(() => { setPage(1); }, [filter]);

  const columns: any[] = [
    { title: '项目', dataIndex: 'project_name', width: 140 },
    {
      title: '计划', dataIndex: 'plan_name', width: 180,
      render: (v: string, row: MyTask) => (
        <Button type="text" onClick={() => history.push(`/plans?plan_id=${row.plan_id}`)}>{v}</Button>
      ),
    },
    { title: '用例', dataIndex: 'case_name', width: 260 },
    {
      title: '状态', dataIndex: 'status', width: 100,
      render: (v: string) => <Tag color={STATUS[v]?.color || 'gray'}>{STATUS[v]?.label || v}</Tag>,
    },
    {
      title: '优先级', dataIndex: 'priority', width: 90,
      render: (v: string) => <Tag color={PRIORITY[v]?.color || 'gray'}>{PRIORITY[v]?.label || v}</Tag>,
    },
    {
      title: '截止日', dataIndex: 'due_date', width: 140,
      render: (v: string, row: MyTask) => (
        <Space>
          <span>{v || '-'}</span>
          {row.overdue && <Tag color="red">已逾期</Tag>}
        </Space>
      ),
    },
  ];

  return (
    <Card>
      <Tabs activeTab={filter} onChange={setFilter}>
        <Tabs.TabPane key="todo" title="待办" />
        <Tabs.TabPane key="overdue" title="已逾期" />
        <Tabs.TabPane key="done" title="已完成" />
      </Tabs>
      <Table
        rowKey="plan_item_id" loading={loading} columns={columns} data={items}
        pagination={{
          current: page, pageSize: size, total, showTotal: true,
          onChange: (p, s) => { setPage(p); setSize(s); },
        }}
        noDataElement={<Empty description={filter === 'done' ? '暂无已完成任务' : '暂无待办任务'} />}
      />
    </Card>
  );
};

export default MyTasks;
