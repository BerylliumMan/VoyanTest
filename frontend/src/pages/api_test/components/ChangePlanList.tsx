import React, { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Button,
  Empty,
  Message,
  Modal,
  Popconfirm,
  Select,
  Space,
  Table,
  Tag,
  Typography,
} from '@arco-design/web-react';
import { IconRefresh } from '@arco-design/web-react/icon';
import { apiGet, apiPost } from '@/utils/apiRequest';

const { Text } = Typography;

export interface ChangePlanListProps {
  visible: boolean;
  projectId?: number | null;
  onClose: () => void;
  onChanged?: () => void;
}

interface AffectedRow {
  definition_id: number;
  method: string;
  path: string;
  case_ids: number[];
  changed_files?: string[];
}

interface ChangePlan {
  id: number;
  plan_id?: number | null;
  plan_name?: string | null;
  status: string;
  decision?: string | null;
  from_commit?: string | null;
  to_commit?: string | null;
  case_count: number;
  affected: AffectedRow[];
  deadline_at?: string | null;
  seconds_left?: number | null;
  retry_count: number;
  batch_id?: number | null;
  reason?: string | null;
  created_at?: string | null;
}

const STATUS_COLOR: Record<string, string> = {
  pending: 'orange',
  running: 'blue',
  done: 'green',
  failed: 'red',
  cancelled: 'gray',
  expired_skipped: 'gray',
};

const STATUS_LABEL: Record<string, string> = {
  pending: '待确认',
  running: '执行中',
  done: '已完成',
  failed: '失败',
  cancelled: '已取消',
  expired_skipped: '已跳过',
};

function formatLeft(seconds: number): string {
  if (seconds <= 0) return '即将自动执行';
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return `${m} 分 ${String(s).padStart(2, '0')} 秒`;
}

function ChangePlanList({ visible, projectId, onClose, onChanged }: ChangePlanListProps) {
  const [loading, setLoading] = useState(false);
  const [items, setItems] = useState<ChangePlan[]>([]);
  const [loadedAt, setLoadedAt] = useState(Date.now());
  const [nowTs, setNowTs] = useState(Date.now());
  const [busyId, setBusyId] = useState<number | null>(null);
  const [statusFilter, setStatusFilter] = useState<string>('');

  const load = useCallback(async () => {
    if (!projectId) return;
    setLoading(true);
    try {
      const qs = statusFilter ? `&status=${statusFilter}` : '';
      const data = await apiGet<{ items: ChangePlan[] }>(
        `/api/api-test/change-plans?project_id=${projectId}&limit=50${qs}`,
      );
      setItems(data?.items || []);
      setLoadedAt(Date.now());
    } catch {
      Message.error('加载变更计划失败');
      setItems([]);
    } finally {
      setLoading(false);
    }
  }, [projectId, statusFilter]);

  useEffect(() => {
    if (visible && projectId) {
      load();
    }
  }, [visible, projectId, load]);

  useEffect(() => {
    if (!visible) return undefined;
    const timer = setInterval(() => setNowTs(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [visible]);

  const remaining = useCallback(
    (row: ChangePlan) => {
      if (row.seconds_left == null) return null;
      const elapsed = Math.floor((nowTs - loadedAt) / 1000);
      return Math.max(0, row.seconds_left - elapsed);
    },
    [nowTs, loadedAt],
  );

  const pendingCount = useMemo(
    () => items.filter((row) => row.status === 'pending').length,
    [items],
  );

  const handleConfirm = async (row: ChangePlan) => {
    setBusyId(row.id);
    try {
      await apiPost(`/api/api-test/change-plans/${row.id}/confirm`, {});
      Message.success('已开始执行受影响用例');
      await load();
      onChanged?.();
    } catch (e: any) {
      Message.error(e?.message || '确认失败');
    } finally {
      setBusyId(null);
    }
  };

  const handleCancel = async (row: ChangePlan) => {
    setBusyId(row.id);
    try {
      await apiPost(`/api/api-test/change-plans/${row.id}/cancel`, { reason: '人工取消' });
      Message.success('已取消');
      await load();
      onChanged?.();
    } catch (e: any) {
      Message.error(e?.message || '取消失败');
    } finally {
      setBusyId(null);
    }
  };

  return (
    <Modal
      title={`变更计划${pendingCount ? `（${pendingCount} 条待确认）` : ''}`}
      visible={visible}
      onCancel={onClose}
      footer={null}
      style={{ width: 900 }}
      unmountOnExit
    >
      <Space style={{ marginBottom: 12 }}>
        <Button icon={<IconRefresh />} onClick={load} loading={loading}>
          刷新
        </Button>
        <Select
          size="small"
          value={statusFilter}
          onChange={setStatusFilter}
          style={{ width: 130 }}
          options={[
            { label: '全部状态', value: '' },
            { label: '待确认', value: 'pending' },
            { label: '执行中', value: 'running' },
            { label: '已完成', value: 'done' },
            { label: '失败', value: 'failed' },
            { label: '已取消', value: 'cancelled' },
            { label: '已跳过', value: 'expired_skipped' },
          ]}
        />
        <Text type="secondary">待确认计划到期后自动执行；执行前可取消。</Text>
      </Space>
      {items.length === 0 && !loading ? (
        <Empty description="暂无变更计划" style={{ padding: '24px 0' }} />
      ) : (
        <Table
        size="small"
        data={items}
        rowKey="id"
        loading={loading}
        pagination={{ pageSize: 10, sizeCanChange: false }}
        expandedRowRender={(row: ChangePlan) => (
          <div style={{ padding: '4px 8px' }}>
            {(row.affected || []).map((item) => (
              <div key={`${row.id}-${item.definition_id}`} style={{ marginBottom: 4 }}>
                <Tag size="small">{item.method}</Tag>
                <Text code>{item.path}</Text>
                <Text type="secondary" style={{ marginLeft: 8 }}>
                  {item.case_ids.length} 条用例
                  {item.changed_files?.length
                    ? ` · 变更文件：${item.changed_files.join(', ')}`
                    : ''}
                </Text>
              </div>
            ))}
            {row.reason ? (
              <Text type="secondary">说明：{row.reason}</Text>
            ) : null}
          </div>
        )}
        columns={[
          {
            title: '状态',
            dataIndex: 'status',
            width: 100,
            render: (value: string, row: ChangePlan) => (
              <Tag color={STATUS_COLOR[value] || 'gray'}>
                {STATUS_LABEL[value] || value}
                {row.status === 'pending' && row.retry_count
                  ? `（重试 ${row.retry_count}）`
                  : ''}
              </Tag>
            ),
          },
          {
            title: '受影响接口 / 用例',
            width: 200,
            render: (_: unknown, row: ChangePlan) =>
              `${(row.affected || []).length} 个接口 / ${row.case_count} 条用例`,
          },
          {
            title: '倒计时',
            width: 160,
            render: (_: unknown, row: ChangePlan) => {
              if (row.status !== 'pending') return '—';
              const left = remaining(row);
              if (left == null) return '—';
              return <Text type={left <= 600 ? 'warning' : undefined}>{formatLeft(left)}</Text>;
            },
          },
          {
            title: '提交',
            width: 170,
            render: (_: unknown, row: ChangePlan) =>
              row.from_commit ? (
                <Text code>
                  {(row.from_commit || '').slice(0, 7)}..{(row.to_commit || '').slice(0, 7)}
                </Text>
              ) : (
                '—'
              ),
          },
          {
            title: '操作',
            width: 170,
            render: (_: unknown, row: ChangePlan) => (
              <Space size="mini">
                {row.status === 'pending' ? (
                  <>
                    <Popconfirm title="立即执行受影响用例？" onOk={() => handleConfirm(row)}>
                      <Button size="mini" type="primary" loading={busyId === row.id}>
                        确认执行
                      </Button>
                    </Popconfirm>
                    <Popconfirm title="取消该变更计划？" onOk={() => handleCancel(row)}>
                      <Button size="mini" loading={busyId === row.id}>
                        取消
                      </Button>
                    </Popconfirm>
                  </>
                ) : row.batch_id ? (
                  <a href={`/reports?batch_id=${row.batch_id}`} target="_blank" rel="noreferrer">
                    批次 #{row.batch_id}
                  </a>
                ) : (
                  '—'
                )}
              </Space>
            ),
          },
        ]}
        />
      )}
    </Modal>
  );
}

export default ChangePlanList;
