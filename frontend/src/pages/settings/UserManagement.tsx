import React, { useEffect, useState } from 'react';
import { Card, Table, Modal, Form, Input, Select, Button, Message, Space } from '@arco-design/web-react';
import { IconPlus, IconEdit } from '@arco-design/web-react/icon';
import axios from 'axios';
import useLocale from '@/utils/useLocale';
import styles from './style/index.module.less';

interface UserInfo {
  id: number;
  username: string;
  role: string;
  status: string;
  project_ids: number[] | null;
}

function UserManagement() {
  const t = useLocale();
  const [users, setUsers] = useState<UserInfo[]>([]);
  const [userLoading, setUserLoading] = useState(false);
  const [userVisible, setUserVisible] = useState(false);
  const [editingUser, setEditingUser] = useState<UserInfo | null>(null);
  const [userForm] = Form.useForm();
  const [projects, setProjects] = useState<{ id: number; name: string }[]>([]);

  const role = Form.useWatch('role', userForm);

  const fetchUsers = () => {
    setUserLoading(true);
    axios
      .get('/api/users/')
      .then((res) => setUsers(res.data || []))
      .catch((err) => Message.error(err?.response?.data?.detail || t['operate.failed']))
      .finally(() => setUserLoading(false));
  };

  useEffect(() => {
    fetchUsers();
    axios
      .get('/api/projects/')
      .then((res) => setProjects(res.data || []))
      .catch(() => {});
  }, []);

  const openUserModal = (user?: UserInfo) => {
    setEditingUser(user || null);
    userForm.resetFields();
    if (user) {
      userForm.setFieldsValue({
        username: user.username,
        role: user.role,
        status: user.status,
        project_ids: user.project_ids || [],
      });
    } else {
      userForm.setFieldsValue({ role: 'tester', project_ids: [] });
    }
    setUserVisible(true);
  };

  const handleUserSubmit = async () => {
    const values = await userForm.validate();
    try {
      if (editingUser) {
        await axios.put(`/api/users/${editingUser.id}`, values);
        Message.success(t['user.updated']);
      } else {
        await axios.post('/api/users/', { ...values, password: values.password });
        Message.success(t['user.created']);
      }
      setUserVisible(false);
      fetchUsers();
    } catch (e: unknown) {
      const err = e as { response?: { data?: { detail?: string } } };
      Message.error(err.response?.data?.detail || t['operate.failed']);
    }
  };

  const handleResetPassword = async (userId: number) => {
    const pw = prompt(t['reset.password.prompt']);
    if (!pw) return;
    try {
      await axios.put(`/api/users/${userId}/reset-password`, { new_password: pw });
      Message.success(t['password.reset']);
    } catch (e: unknown) {
      const err = e as { response?: { data?: { detail?: string } } };
      Message.error(err.response?.data?.detail || t['reset.failed']);
    }
  };

  const userColumns = [
    { title: t['username'], dataIndex: 'username' },
    { title: t['role'], dataIndex: 'role', width: 100 },
    { title: t['status'], dataIndex: 'status', width: 100 },
    {
      title: t['actions'],
      width: 180,
      render: (_: unknown, r: UserInfo) => (
        <Space>
          <Button type="text" size="small" icon={<IconEdit />} onClick={() => openUserModal(r)}>
            {t['edit']}
          </Button>
          <Button type="text" size="small" onClick={() => handleResetPassword(r.id)}>
            {t['reset.password']}
          </Button>
        </Space>
      ),
    },
  ];

  return (
    <Card>
      <Space className={styles.actionsRow}>
        <Button type="primary" icon={<IconPlus />} onClick={() => openUserModal()}>
          {t['new.user']}
        </Button>
      </Space>
      <Table columns={userColumns} data={users} rowKey="id" loading={userLoading} pagination={false} />

      <Modal
        visible={userVisible}
        onCancel={() => setUserVisible(false)}
        title={editingUser ? t['edit.user'] : t['new.user']}
        onOk={handleUserSubmit}
      >
        <Form form={userForm} layout="vertical">
          <Form.Item field="username" label={t['username']} rules={[{ required: true }]}>
            <Input placeholder={t['username']} disabled={!!editingUser} />
          </Form.Item>
          {!editingUser && (
            <Form.Item field="password" label={t['password']} rules={[{ required: true, minLength: 8, message: '密码至少8位，需包含字母、数字和特殊字符' }]}>
              <Input.Password placeholder={t['password.placeholder']} />
            </Form.Item>
          )}
          <Form.Item field="role" label={t['role']}>
            <Select
              options={[
                { label: t['admin'], value: 'admin' },
                { label: t['tester'], value: 'tester' },
              ]}
            />
          </Form.Item>
          {role !== 'admin' && (
            <Form.Item field="project_ids" label={t['user.accessible_projects']}>
              <Select
                mode="multiple"
                allowClear
                placeholder={t['user.accessible_projects']}
                options={projects.map((p) => ({ label: p.name, value: p.id }))}
              />
            </Form.Item>
          )}
          {editingUser && (
            <Form.Item field="status" label={t['status']}>
              <Select
                options={[
                  { label: t['enabled'], value: 'active' },
                  { label: t['disabled'], value: 'disabled' },
                ]}
              />
            </Form.Item>
          )}
        </Form>
      </Modal>
    </Card>
  );
}

export default UserManagement;
