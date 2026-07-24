import React, { useEffect, useState } from 'react';
import { Modal, Table, Button, Message, Badge, Spin } from '@arco-design/web-react';
import axios from 'axios';
import useLocale from '@/utils/useLocale';

interface VersionItem {
  id: number;
  version: number;
  is_active: boolean;
  description: string | null;
  updated_at: string | null;
}

interface Props {
  promptKey: string;
  visible: boolean;
  onClose: () => void;
}

function PromptVersionHistory({ promptKey, visible, onClose }: Props) {
  const t = useLocale();
  const [loading, setLoading] = useState(false);
  const [versions, setVersions] = useState<VersionItem[]>([]);
  const [activating, setActivating] = useState<number | null>(null);

  const fetchVersions = () => {
    if (!promptKey) return;
    setLoading(true);
    axios
      .get(`/api/config/prompts/${promptKey}/versions`)
      .then((res) => setVersions(res.data || []))
      .catch((err) => Message.error(err?.response?.data?.detail || t['operate.failed']))
      .finally(() => setLoading(false));
  };

  useEffect(() => {
    if (visible) fetchVersions();
  }, [visible, promptKey]);

  const handleActivate = async (version: number) => {
    setActivating(version);
    try {
      await axios.put(`/api/config/prompts/${promptKey}/activate`, { version });
      Message.success(t['save.success'] || '已激活');
      fetchVersions();
    } catch (e: unknown) {
      const err = e as { response?: { data?: { detail?: string } } };
      Message.error(err.response?.data?.detail || t['operate.failed']);
    } finally {
      setActivating(null);
    }
  };

  const columns = [
    {
      title: '版本',
      dataIndex: 'version',
      width: 80,
      render: (v: number) => `v${v}`,
    },
    {
      title: '状态',
      dataIndex: 'is_active',
      width: 80,
      render: (v: boolean) =>
        v ? <Badge status="success" text="使用中" /> : <Badge status="default" text="历史" />,
    },
    {
      title: '描述',
      dataIndex: 'description',
      ellipsis: true,
      render: (v: string | null) => v || '-',
    },
    {
      title: '更新时间',
      dataIndex: 'updated_at',
      width: 180,
      render: (v: string | null) => (v ? new Date(v).toLocaleString() : '-'),
    },
    {
      title: '操作',
      width: 80,
      render: (_: unknown, record: VersionItem) =>
        record.is_active ? null : (
          <Button
            type="text"
            size="small"
            loading={activating === record.version}
            onClick={() => handleActivate(record.version)}
          >
            激活
          </Button>
        ),
    },
  ];

  return (
    <Modal
      title="版本历史"
      visible={visible}
      onCancel={onClose}
      footer={<Button onClick={onClose}>关闭</Button>}
      style={{ width: 700 }}
    >
      <Spin loading={loading}>
        <Table
          data={versions}
          columns={columns}
          rowKey="id"
          pagination={false}
          size="small"
        />
      </Spin>
    </Modal>
  );
}

export default PromptVersionHistory;
