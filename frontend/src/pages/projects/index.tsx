import React, { useEffect, useState, useCallback } from 'react';
import {
  Card, Table, Button, Modal, Form, Input, Message, Spin,
  Space, Popconfirm, Select, Switch,
} from '@arco-design/web-react';
import { IconPlus, IconEdit, IconDelete } from '@arco-design/web-react/icon';
import axios from 'axios';
import useLocale from '@/utils/useLocale';
import EnvironmentManager from './EnvironmentManager';
import styles from './style/index.module.less';

interface Project { id: number; name: string; description: string; base_url: string; browser: string; headless: boolean; }

const Projects: React.FC = () => {
  const t = useLocale();
  const [data, setData] = useState<Project[]>([]);
  const [loading, setLoading] = useState(false);
  const [visible, setVisible] = useState(false);
  const [editing, setEditing] = useState<Project | null>(null);
  const [form] = Form.useForm();
  const [headless, setHeadless] = useState(true);  // Switch 显式控制
  const [pageSize, setPageSize] = useState(20);

  const fetchData = useCallback(() => {
    setLoading(true);
    axios.get('/api/projects/').then((res) => setData(res.data || [])).catch((err) => Message.error(err?.response?.data?.detail || t['operate.failed'])).finally(() => setLoading(false));
  }, []);

  useEffect(() => { fetchData(); }, [fetchData]);

  const openCreate = () => {
    setEditing(null);
    setHeadless(true);
    form.resetFields();
    form.setFieldsValue({ browser: 'chromium', headless: true });
    setVisible(true);
  };

  const openEdit = (project: Project) => {
    setEditing(project);
    setHeadless(project.headless !== false);
    form.setFieldsValue(project);
    setVisible(true);
  };

  const handleSubmit = async () => {
    const values = await form.validate();
    values.headless = headless;
    try {
      if (editing) {
        await axios.put(`/api/projects/${editing.id}`, values);
        Message.success(t['update.success']);
      } else {
        await axios.post('/api/projects/', values);
        Message.success(t['create.success']);
      }
      setVisible(false);
      fetchData();
    } catch (e: unknown) {
      const err = e as { response?: { data?: { detail?: string } } };
      Message.error(err.response?.data?.detail || t['operate.failed']);
    }
  };

  const handleDelete = async (id: number) => {
    try {
      await axios.delete(`/api/projects/${id}`);
      Message.success(t['deleted']);
      fetchData();
    } catch (e: unknown) {
      const err = e as { response?: { data?: { detail?: string } } };
      Message.error(err.response?.data?.detail || t['operate.failed']);
    }
  };

  const columns = [
    { title: 'ID', dataIndex: 'id', width: 60 },
    { title: t['name'], dataIndex: 'name' },
    { title: t['description'], dataIndex: 'description', ellipsis: true },
    { title: t['base.url'], dataIndex: 'base_url', ellipsis: true },
    { title: t['browser'], dataIndex: 'browser', width: 100 },
    {
      title: t['headless'], dataIndex: 'headless', width: 80,
      render: (v: boolean) => v ? t['yes'] : t['no'],
    },
    {
      title: t['actions'], width: 140,
      render: (_: unknown, record: Project) => (
        <Space>
          <Button type="text" size="small" icon={<IconEdit />} onClick={() => openEdit(record)} aria-label="编辑" />
          <Popconfirm title={t['confirm.delete']} onOk={() => handleDelete(record.id)}>
            <Button type="text" size="small" status="danger" icon={<IconDelete />} aria-label="删除" />
          </Popconfirm>
        </Space>
      ),
    },
  ];

  return (
    <div>
      <div className={styles['page-header']}>
        <Button type="primary" icon={<IconPlus />} onClick={openCreate}>{t['new.project']}</Button>
      </div>
      <Card>
        <Spin loading={loading}>
          <Table columns={columns} data={data} rowKey="id" stripe pagination={{
            pageSize,
            onChange: (_, ps) => setPageSize(ps),
            sizeOptions: [10, 20, 50],
            sizeCanChange: true,
            showTotal: true,
          }} />
        </Spin>
      </Card>

      {/* Project Edit Modal */}
      <Modal visible={visible} onCancel={() => setVisible(false)}
        title={editing ? t['edit.project'] : t['new.project']} onOk={handleSubmit} className={styles.projectFormModal}
      >
        <Form form={form} layout="vertical">
          <Form.Item field="name" label={t['project.name']} rules={[{ required: true, message: t['project.name.placeholder'] }]}>
            <Input placeholder={t['project.name']} />
          </Form.Item>
          <Form.Item field="base_url" label={t['base.url']} rules={[{ required: true, message: t['base.url.required'] }]}>
            <Input placeholder="https://example.com" />
          </Form.Item>
          <Form.Item field="description" label={t['description']}>
            <Input.TextArea placeholder={t['description']} rows={3} />
          </Form.Item>
          <Space size="large">
            <Form.Item field="browser" label={t['browser']}>
              <Select options={[
                { label: 'Chromium', value: 'chromium' },
                { label: 'Firefox', value: 'firefox' },
                { label: 'WebKit', value: 'webkit' },
              ]} className={styles.browserSelect} />
            </Form.Item>
            <Form.Item field="headless" label={t['headless']}>
              <Switch checked={headless} onChange={setHeadless} />
            </Form.Item>
          </Space>
        </Form>

        {editing && <EnvironmentManager projectId={editing.id} />}
      </Modal>
    </div>
  );
};

export default Projects;
