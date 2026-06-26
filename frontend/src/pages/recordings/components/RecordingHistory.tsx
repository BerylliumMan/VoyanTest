import React, { useState, useEffect } from 'react';
import {
  Table,
  Button,
  Tag,
  Space,
  Modal,
  Message,
} from '@arco-design/web-react';
import {
  IconDelete,
  IconHistory,
  IconRefresh,
} from '@arco-design/web-react/icon';
import { apiGet, apiDelete } from '@/utils/apiRequest';

interface HistorySession {
  session_id: string;
  status: string;
  url: string;
  page_title: string;
  elapsed_seconds: number;
  events_count: number;
}

interface RecordingHistoryProps {
  onLoadSession: (sessionId: string) => void;
}

const RecordingHistory: React.FC<RecordingHistoryProps> = ({ onLoadSession }) => {
  const [sessions, setSessions] = useState<HistorySession[]>([]);
  const [loading, setLoading] = useState(false);

  const loadHistory = async () => {
    setLoading(true);
    try {
      const data = await apiGet<{ sessions: HistorySession[] }>('/api/recordings/history');
      setSessions(data.sessions || []);
    } catch {
      // silent
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadHistory();
  }, []);

  const handleDelete = (sessionId: string) => {
    Modal.confirm({
      title: '删除录制历史',
      content: '确定删除此录制会话记录？',
      onOk: async () => {
        try {
          await apiDelete(`/api/recordings/${sessionId}/history`);
          Message.success('已删除');
          loadHistory();
        } catch {
          Message.error('删除失败');
        }
      },
    });
  };

  const columns = [
    {
      title: 'URL',
      dataIndex: 'url',
      ellipsis: true,
      render: (v: string) => v || '-',
    },
    {
      title: '状态',
      dataIndex: 'status',
      width: 100,
      render: (v: string) => (
        <Tag color={v === 'recording' ? 'red' : 'gray'}>
          {v === 'recording' ? '录制中' : '已停止'}
        </Tag>
      ),
    },
    {
      title: '事件数',
      dataIndex: 'events_count',
      width: 80,
    },
    {
      title: '操作',
      width: 160,
      render: (_: any, record: HistorySession) => (
        <Space>
          <Button
            size="small"
            type="primary"
            onClick={() => onLoadSession(record.session_id)}
          >
            重新加载
          </Button>
          <Button
            size="small"
            status="danger"
            icon={<IconDelete />}
            onClick={() => handleDelete(record.session_id)}
          />
        </Space>
      ),
    },
  ];

  return (
    <div>
      <Space style={{ marginBottom: 16 }}>
        <IconHistory />
        <strong>历史录制</strong>
        <Button size="small" icon={<IconRefresh />} onClick={loadHistory} loading={loading}>
          刷新
        </Button>
      </Space>
      <Table
        columns={columns}
        data={sessions}
        rowKey="session_id"
        pagination={{ pageSize: 10 }}
        loading={loading}
      />
    </div>
  );
};

export default RecordingHistory;
