import React, { useCallback, useEffect, useState } from 'react';
import {
  Table, Modal, Form, Input, Select, Switch, Button, Spin,
  Message, Popconfirm, Tag, Space, Collapse,
} from '@arco-design/web-react';
import { IconPlus, IconEdit, IconDelete, IconCheck } from '@arco-design/web-react/icon';
import axios from 'axios';
import useLocale from '@/utils/useLocale';

// Arco Design Form.Item/Input.Password 支持 valuePropName/showEyeButton 但未在类型中暴露，
// 这里用宽松类型绕过类型检查
const FormItem = Form.Item as unknown as React.FC<
  Record<string, unknown> & { children?: React.ReactNode }
>;
const PasswordInput = Input.Password as unknown as React.FC<
  Record<string, unknown>
>;

interface Environment {
  id: number;
  name: string;
  base_url: string;
  browser: string;
  headless: boolean;
  is_default: boolean;
  project_id: number;
  cookies?: Array<{ name: string; value: string; domain?: string }>;
}

interface EnvironmentManagerProps {
  projectId: number;
}

const EnvironmentManager: React.FC<EnvironmentManagerProps> = ({ projectId }) => {
  const t = useLocale();
  const [environments, setEnvironments] = useState<Environment[]>([]);
  const [envLoading, setEnvLoading] = useState(false);
  const [envModalVisible, setEnvModalVisible] = useState(false);
  const [editingEnv, setEditingEnv] = useState<Environment | null>(null);
  const [envForm] = Form.useForm();

  const fetchEnvironments = useCallback(async (pid: number) => {
    setEnvLoading(true);
    try {
      const res = await axios.get(`/api/projects/${pid}/environments`);
      setEnvironments(res.data || []);
    } finally {
      setEnvLoading(false);
    }
  }, []);

  // Fetch on mount
  useEffect(() => {
    fetchEnvironments(projectId);
  }, [fetchEnvironments, projectId]);

  const openCreateEnv = () => {
    setEditingEnv(null);
    envForm.resetFields();
    envForm.setFieldsValue({ browser: 'chromium', headless: true, cookies: [] });
    setEnvModalVisible(true);
  };

  const openEditEnv = (env: Environment) => {
    setEditingEnv(env);
    envForm.setFieldsValue(env);
    setEnvModalVisible(true);
  };

  const handleEnvSubmit = async () => {
    const values = await envForm.validate();
    try {
      if (editingEnv) {
        await axios.put(`/api/environments/${editingEnv.id}`, values);
        Message.success(t['environment.update_success']);
      } else {
        await axios.post(`/api/projects/${projectId}/environments`, values);
        Message.success(t['environment.create_success']);
      }
      setEnvModalVisible(false);
      fetchEnvironments(projectId);
    } catch (e: unknown) {
      const err = e as { response?: { data?: { detail?: string } } };
      Message.error(err.response?.data?.detail || t['operate.failed']);
    }
  };

  const handleDeleteEnv = async (id: number) => {
    try {
      await axios.delete(`/api/environments/${id}`);
      Message.success(t['environment.delete_success']);
      fetchEnvironments(projectId);
    } catch (e: unknown) {
      const err = e as { response?: { data?: { detail?: string } } };
      Message.error(err.response?.data?.detail || t['operate.failed']);
    }
  };

  const handleSetDefaultEnv = async (id: number) => {
    try {
      await axios.put(`/api/environments/${id}/default`);
      Message.success(t['environment.set_default_success']);
      fetchEnvironments(projectId);
    } catch (e: unknown) {
      const err = e as { response?: { data?: { detail?: string } } };
      Message.error(err.response?.data?.detail || t['operate.failed']);
    }
  };

  const envColumns = [
    { title: t['environment.form.name'], dataIndex: 'name', width: 120 },
    { title: t['environment.form.base_url'], dataIndex: 'base_url', ellipsis: true },
    { title: t['environment.form.browser'], dataIndex: 'browser', width: 90 },
    {
      title: t['environment.is_default'], dataIndex: 'is_default', width: 80,
      render: (v: boolean) => v ? <Tag color="green">{t['yes']}</Tag> : null,
    },
    {
      title: t['actions'], width: 200,
      render: (_: unknown, record: Environment) => (
        <Space>
          {!record.is_default && (
            <Button size="mini" type="secondary" icon={<IconCheck />}
              onClick={() => handleSetDefaultEnv(record.id)}
            >{t['environment.set_default']}</Button>
          )}
          <Button size="mini" type="text" icon={<IconEdit />} onClick={() => openEditEnv(record)} aria-label="编辑环境" />
            <Popconfirm
              title={t['environment.delete.confirm'].replace('{name}', record.name)}
              onOk={() => handleDeleteEnv(record.id)}
            >
              <Button size="mini" type="text" status="danger" icon={<IconDelete />} aria-label="删除环境" />
            </Popconfirm>
        </Space>
      ),
    },
  ];

  return (
    <>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 12 }}>
        <strong>{t['environments']}</strong>
        <Button size="small" type="primary" icon={<IconPlus />} onClick={openCreateEnv}>{t['environment.new']}</Button>
      </div>
      <Spin loading={envLoading}>
        <Table columns={envColumns} data={environments} rowKey="id" pagination={false} />
      </Spin>

      {/* Environment Edit Modal */}
      <Modal visible={envModalVisible} onCancel={() => setEnvModalVisible(false)}
        title={editingEnv ? t['environment.edit'] : t['environment.new']}
        onOk={handleEnvSubmit} style={{ width: 500 }}
      >
        <Form form={envForm} layout="vertical">
          <Form.Item field="name" label={t['environment.form.name']}
            rules={[{ required: true, message: t['environment.name.placeholder'] }]}
          >
            <Input placeholder={t['environment.name.placeholder']} />
          </Form.Item>
          <Form.Item field="base_url" label={t['environment.form.base_url']}
            rules={[{ required: true, message: t['environment.base_url'] }]}
          >
            <Input placeholder="https://example.com" />
          </Form.Item>
          <Space size="large">
            <Form.Item field="browser" label={t['environment.form.browser']}>
              <Select options={[
                { label: 'Chromium', value: 'chromium' },
                { label: 'Firefox', value: 'firefox' },
                { label: 'WebKit', value: 'webkit' },
              ]} style={{ width: 120 }} />
            </Form.Item>
            <FormItem field="headless" label={t['environment.form.headless']} initialValue={true} valuePropName="checked">
              <Switch />
            </FormItem>
          </Space>
          <Collapse style={{ marginBottom: 0 }}>
            <Collapse.Item header="认证 Cookie" name="cookies">
              <Form.List field="cookies">
                {(fields, { add, remove }) => (
                  <div>
                    {fields.map((field, index) => (
                      <div key={field.key} style={{ display: 'flex', gap: 8, marginBottom: 8, alignItems: 'center' }}>
                        <Form.Item
                          {...field}
                          field={`${field.field}.name`}
                          rules={[{ required: true, message: '请输入 Cookie 名称' }]}
                          noStyle
                        >
                          <Input placeholder="Cookie 名称" style={{ flex: 1 }} />
                        </Form.Item>
                        <FormItem
                          {...field}
                          field={`${field.field}.value`}
                          rules={[{ required: true, message: '请输入 Cookie 值' }]}
                          noStyle
                        >
                          <PasswordInput placeholder="Cookie 值" style={{ flex: 1 }} showEyeButton />
                        </FormItem>
                        <Form.Item
                          {...field}
                          field={`${field.field}.domain`}
                          noStyle
                        >
                          <Input placeholder="可不填" style={{ flex: 1 }} />
                        </Form.Item>
                        <Button
                          type="text"
                          status="danger"
                          icon={<IconDelete />}
                          onClick={() => remove(index)}
                          aria-label="删除 Cookie"
                        />
                      </div>
                    ))}
                    <Button
                      type="dashed"
                      long
                      icon={<IconPlus />}
                      onClick={() => add({ name: '', value: '', domain: '' })}
                    >
                      添加 Cookie
                    </Button>
                  </div>
                )}
              </Form.List>
            </Collapse.Item>
          </Collapse>
        </Form>
      </Modal>
    </>
  );
};

export default EnvironmentManager;
