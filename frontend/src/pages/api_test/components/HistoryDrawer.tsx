import React, { useCallback, useEffect, useState } from 'react';
import { Button, Drawer, Empty, Message, Popconfirm, Space, Spin, Table, Tag } from '@arco-design/web-react';
import { IconDelete } from '@arco-design/web-react/icon';
import { apiDelete, apiGet } from '@/utils/apiRequest';
import { RequestHistory } from '../definitions/types';

interface HistoryDrawerProps {
  visible: boolean;
  projectId?: number | null;
  onCancel: () => void;
  /** 回填：把历史中的方法 + URL（模板态）写回当前编辑器 */
  onPick: (row: RequestHistory) => void;
}

const statusColor = (status: number | null): string => {
  if (status == null) return 'red';
  if (status >= 200 && status < 300) return 'green';
  if (status >= 300 && status < 400) return 'arcoblue';
  if (status >= 400 && status < 500) return 'orange';
  return 'red';
};

/**
 * 031（US7 T030）调试请求历史抽屉。
 *
 * 契约（contracts §1.2/§1.3）：服务端持久化、按用户隔离、倒序、可清空；
 * 历史里的 headers/body 是**脱敏后**的发送内容，URL 是模板态 —— 因此“回填”只恢复
 * 方法与 URL（请求头/正文可能含 ****** 占位，由使用者在编辑器中维护）。
 */
const HistoryDrawer: React.FC<HistoryDrawerProps> = ({ visible, projectId, onCancel, onPick }) => {
  const [items, setItems] = useState<RequestHistory[]>([]);
  const [loading, setLoading] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const data = await apiGet<{ total: number; items: RequestHistory[] }>(
        '/api/api-test/history',
        projectId ? { project_id: projectId, limit: 50 } : { limit: 50 }
      );
      setItems(Array.isArray(data) ? data : data?.items ?? []);
    } catch {
      setItems([]);
    } finally {
      setLoading(false);
    }
  }, [projectId]);

  useEffect(() => {
    if (visible) load();
  }, [visible, load]);

  const handleClear = async () => {
    try {
      await apiDelete(
        `/api/api-test/history${projectId ? `?project_id=${projectId}` : ''}`,
        '历史已清空'
      );
      await load();
    } catch {
      /* 已弹错误 */
    }
  };

  return (
    <Drawer
      title="请求历史"
      visible={visible}
      onCancel={onCancel}
      width={720}
      footer={null}
      unmountOnExit
    >
      <Space style={{ marginBottom: 12 }}>
        <Popconfirm title="清空当前项目的历史记录？" onOk={handleClear}>
          <Button size="small" status="danger" icon={<IconDelete />}>
            清空
          </Button>
        </Popconfirm>
        <Button size="small" onClick={load}>
          刷新
        </Button>
        <span style={{ fontSize: 12, color: 'var(--vt-text-3)' }}>
          点击行回填「方法 + URL（模板态）」；请求头/正文为脱敏后的发送内容
        </span>
      </Space>

      <Spin loading={loading} style={{ width: '100%' }}>
        {items.length === 0 && !loading ? (
          <Empty description="暂无历史（真实发送后自动记录，dry-run 不记录）" />
        ) : (
          <Table
            rowKey="id"
            size="small"
            data={items}
            pagination={{ pageSize: 20 }}
            scroll={{ y: 420 }}
            onRow={(record) => ({
              onClick: () => {
                onPick(record as RequestHistory);
                Message.info('已回填方法与 URL；请求头/正文请在编辑器中维护');
              },
              style: { cursor: 'pointer' },
            })}
            columns={[
              {
                title: '方法',
                dataIndex: 'method',
                width: 80,
                render: (v: string) => <Tag color="arcoblue">{v}</Tag>,
              },
              { title: 'URL（模板态）', dataIndex: 'url', ellipsis: true },
              {
                title: '状态',
                dataIndex: 'status_code',
                width: 90,
                render: (v: number | null, row: RequestHistory) =>
                  v == null ? (
                    <Tag color="red">失败</Tag>
                  ) : (
                    <Tag color={statusColor(v)}>{v}</Tag>
                  ),
              },
              {
                title: '耗时',
                dataIndex: 'duration_ms',
                width: 90,
                render: (v: number | null) => (v == null ? '—' : `${v} ms`),
              },
              {
                title: '时间',
                dataIndex: 'created_at',
                width: 160,
                render: (v: string | null) => (v ? new Date(v).toLocaleString() : '—'),
              },
            ]}
          />
        )}
      </Spin>
    </Drawer>
  );
};

export default HistoryDrawer;
