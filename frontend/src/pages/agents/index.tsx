import React, { useEffect, useState } from 'react';
import { Card, Table, Button, Modal, Form, Input, Message, Tag, Space, Popconfirm, Typography } from '@arco-design/web-react';
import { IconPlus, IconEdit, IconDelete } from '@arco-design/web-react/icon';
import axios from 'axios';
import useLocale from '@/utils/useLocale';
import styles from './style/index.module.less';

interface AgentItem { id: number | null; name: string; status: string; endpoint: string; description: string; hostname?: string; }
interface RunRecord { id: number; run_id: number; batch_id: number; testcase_name: string; status: string; created_at?: string; logs?: string; level?: string; message?: string; }

function Agents() {
  const t = useLocale();
  const [agents, setAgents] = useState<AgentItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [visible, setVisible] = useState(false);
  const [editingAgent, setEditingAgent] = useState<AgentItem | null>(null);
  const [form] = Form.useForm();
  const [expandedRowKeys, setExpandedRowKeys] = useState<string[]>([]);
  const [logs, setLogs] = useState<Record<number, RunRecord[]>>({});

  const fetchAgents = () => {
    setLoading(true);
    axios.get('/api/agents').then((res) => setAgents(res.data || [])).catch((err) => Message.error(err?.response?.data?.detail || t['operate.failed'])).finally(() => setLoading(false));
  };

  useEffect(() => { fetchAgents(); }, []);

  const openModal = (agent?: AgentItem) => {
    setEditingAgent(agent || null);
    form.resetFields();
    if (agent) form.setFieldsValue({ name: agent.name, endpoint: agent.endpoint, description: agent.description });
    setVisible(true);
  };

  const handleSubmit = async () => {
    const values = await form.validate();
    try {
      if (editingAgent) {
        await axios.put(`/api/agents/${editingAgent.id}`, values);
        Message.success(t['update.success']);
      } else {
        await axios.post('/api/agents/register', values);
        Message.success(t['create.success']);
      }
      setVisible(false);
      fetchAgents();
    } catch (e: unknown) {
      const err = e as { response?: { data?: { detail?: string } } };
      Message.error(err.response?.data?.detail || t['operate.failed']);
    }
  };

  const handleDelete = async (agent: AgentItem) => {
    try {
      await axios.delete(`/api/agents/${agent.id}`);
      Message.success(t['deleted']);
      fetchAgents();
    } catch (e: unknown) {
      const err = e as { response?: { data?: { detail?: string } } };
      Message.error(err.response?.data?.detail || t['operate.failed']);
    }
  };

  const fetchLogs = async (agentId: number) => {
    if (logs[agentId]) return;
    try {
      const res = await axios.get(`/api/agents/${agentId}/logs`, { params: { page: 1, size: 50 } });
      setLogs((prev: Record<number, RunRecord[]>) => ({ ...prev, [agentId]: res.data.items || [] }));
    } catch (e: unknown) { console.error('Failed to load agent logs:', e); }
  };

  const statusColorMap: Record<string, string> = {
    online: 'green',
    offline: 'gray',
    busy: 'orange',
  };

  const columns = [
    { title: t['agent.name'], dataIndex: 'name', width: 150 },
    { title: t['agent.endpoint'], dataIndex: 'endpoint', width: 250 },
    {
      title: t['status'], dataIndex: 'status', width: 100,
      render: (v: string) => <Tag color={statusColorMap[v] || 'gray'}>{t[`agent.status.${v}`] || v}</Tag>,
    },
    { title: t['description'], dataIndex: 'description', ellipsis: true },
    {
      title: t['agent.last_heartbeat'], dataIndex: 'last_heartbeat', width: 170,
      render: (v: string) => v ? new Date(v).toLocaleString() : '-',
    },
    {
      title: t['actions'], width: 140,
      render: (_: unknown, r: AgentItem) => (
        <Space>
          <Button type="text" size="small" icon={<IconEdit />} onClick={() => openModal(r)}>{t['edit']}</Button>
          <Popconfirm title={t['agent.delete.confirm'].replace('{name}', r.name)} onOk={() => handleDelete(r)}>
            <Button type="text" size="small" icon={<IconDelete />}>{t['delete']}</Button>
          </Popconfirm>
        </Space>
      ),
    },
  ];

  return (
    <div>
      <Card>
        <div className={styles.toolbar}>
          <Button type="primary" icon={<IconPlus />} onClick={() => openModal()}>{t['agent.register']}</Button>
        </div>
        <Table
          columns={columns}
          data={agents}
          rowKey="id"
          loading={loading}
          pagination={false}
          expandedRowKeys={expandedRowKeys}
          onExpand={(record: AgentItem, opened: boolean) => {
            if (opened) {
              setExpandedRowKeys([String(record.id)]);
              fetchLogs(Number(record.id));
            } else {
              setExpandedRowKeys([]);
            }
          }}
          expandedRowRender={(r: AgentItem) => {
            const agentLogs = r.id != null ? logs[r.id] || [] : [];
            return (
              <div className={styles['log-section']}>
                <Typography.Text bold className={styles['log-section-title']}>{t['agent.recent_logs']}</Typography.Text>
                {agentLogs.length === 0 ? (
                  <Typography.Text type="secondary">{t['no.data']}</Typography.Text>
                ) : (
                  agentLogs.map((log: RunRecord) => (
                    <div key={log.id} className={styles['log-item']}>
                      <Tag color={log.level === 'error' ? 'red' : log.level === 'warn' ? 'orange' : 'blue'} className={styles['log-level-tag']}>{log.level}</Tag>
                      <span>{log.created_at ? new Date(log.created_at).toLocaleString() : ''}</span>
                      <span className={styles['log-time']}>{log.message}</span>
                    </div>
                  ))
                )}
              </div>
            );
          }}
        />
      </Card>

      <Modal
        visible={visible}
        onCancel={() => setVisible(false)}
        title={editingAgent ? t['agent.edit'] : t['agent.register']}
        onOk={handleSubmit}
      >
        <Form form={form} layout="vertical">
          <Form.Item field="name" label={t['agent.name']} rules={[{ required: true }]}>
            <Input placeholder={t['agent.name.placeholder']} disabled={!!editingAgent} />
          </Form.Item>
          <Form.Item field="endpoint" label={t['agent.endpoint']} rules={[{ required: true }]}>
            <Input placeholder={t['agent.endpoint.placeholder']} />
          </Form.Item>
          <Form.Item field="description" label={t['description']}>
            <Input.TextArea placeholder={t['description']} />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
}

export default Agents;
