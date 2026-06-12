import React, { useEffect, useState } from 'react';
import { Card, Table, Modal, Form, Input, Select, Switch, Button, Message, Space, Popconfirm } from '@arco-design/web-react';
import { IconPlus, IconEdit, IconDelete } from '@arco-design/web-react/icon';
import axios from 'axios';
import useLocale from '@/utils/useLocale';

interface ScheduleInfo {
  id: number;
  name: string;
  cron_expression: string;
  enabled: boolean;
  project_id: number;
  task_type: string;
  target_id: number;
  description: string;
}

function ScheduleManagement() {
  const t = useLocale();
  const [schedules, setSchedules] = useState<ScheduleInfo[]>([]);
  const [scheduleLoading, setScheduleLoading] = useState(false);
  const [scheduleVisible, setScheduleVisible] = useState(false);
  const [editingSchedule, setEditingSchedule] = useState<ScheduleInfo | null>(null);
  const [scheduleForm] = Form.useForm();

  const fetchSchedules = () => {
    setScheduleLoading(true);
    axios
      .get('/api/schedules')
      .then((res) => setSchedules(res.data || []))
      .catch((err) => Message.error(err?.response?.data?.detail || t['operate.failed']))
      .finally(() => setScheduleLoading(false));
  };

  useEffect(() => {
    fetchSchedules();
  }, []);

  const openScheduleModal = (schedule?: ScheduleInfo) => {
    setEditingSchedule(schedule || null);
    scheduleForm.resetFields();
    if (schedule) {
      scheduleForm.setFieldsValue({
        name: schedule.name,
        cron_expression: schedule.cron_expression,
        task_type: schedule.task_type,
        target_id: schedule.target_id,
        description: schedule.description,
        enabled: schedule.enabled,
      });
    }
    setScheduleVisible(true);
  };

  const handleScheduleSubmit = async () => {
    const values = await scheduleForm.validate();
    try {
      if (editingSchedule) {
        await axios.put(`/api/schedules/${editingSchedule.id}`, values);
        Message.success(t['schedule.update_success']);
      } else {
        await axios.post('/api/schedules', values);
        Message.success(t['schedule.create_success']);
      }
      setScheduleVisible(false);
      fetchSchedules();
    } catch (e: unknown) {
      const err = e as { response?: { data?: { detail?: string } } };
      Message.error(err.response?.data?.detail || t['operate.failed']);
    }
  };

  const handleToggleSchedule = async (schedule: ScheduleInfo) => {
    try {
      await axios.put(`/api/schedules/${schedule.id}/toggle`);
      Message.success(t['schedule.toggle_success']);
      fetchSchedules();
    } catch (e: unknown) {
      const err = e as { response?: { data?: { detail?: string } } };
      Message.error(err.response?.data?.detail || t['operate.failed']);
    }
  };

  const handleDeleteSchedule = async (schedule: ScheduleInfo) => {
    try {
      await axios.delete(`/api/schedules/${schedule.id}`);
      Message.success(t['schedule.delete_success']);
      fetchSchedules();
    } catch (e: unknown) {
      const err = e as { response?: { data?: { detail?: string } } };
      Message.error(err.response?.data?.detail || t['operate.failed']);
    }
  };

  const scheduleColumns = [
    { title: t['schedule.name'], dataIndex: 'name' },
    { title: t['schedule.cron'], dataIndex: 'cron_expression', width: 140 },
    {
      title: t['schedule.task_type'],
      dataIndex: 'task_type',
      width: 100,
      render: (v: string) => t[`schedule.task_type.${v}`] || v,
    },
    { title: t['schedule.target_id'], dataIndex: 'target_id', width: 80 },
    {
      title: t['schedule.enabled'],
      dataIndex: 'enabled',
      width: 80,
      render: (v: boolean, r: ScheduleInfo) => (
        <Switch checked={v} onChange={() => handleToggleSchedule(r)} />
      ),
    },
    {
      title: t['schedule.last_run'],
      dataIndex: 'last_run_at',
      width: 170,
      render: (v: string) => (v ? new Date(v).toLocaleString() : '-'),
    },
    {
      title: t['schedule.next_run'],
      dataIndex: 'next_run_at',
      width: 170,
      render: (v: string) => (v ? new Date(v).toLocaleString() : '-'),
    },
    { title: t['schedule.run_count'], dataIndex: 'run_count', width: 80 },
    {
      title: t['actions'],
      width: 120,
      render: (_: unknown, r: ScheduleInfo) => (
        <Space>
          <Button type="text" size="small" icon={<IconEdit />} onClick={() => openScheduleModal(r)}>
            {t['edit']}
          </Button>
          <Popconfirm
            title={t['schedule.delete.confirm'].replace('{name}', r.name)}
            onOk={() => handleDeleteSchedule(r)}
          >
            <Button type="text" size="small" icon={<IconDelete />}>
              {t['delete']}
            </Button>
          </Popconfirm>
        </Space>
      ),
    },
  ];

  return (
    <Card>
      <div style={{ marginBottom: 16 }}>
        <Button type="primary" icon={<IconPlus />} onClick={() => openScheduleModal()}>
          {t['schedule.new']}
        </Button>
      </div>
      <Table
        columns={scheduleColumns}
        data={schedules}
        rowKey="id"
        loading={scheduleLoading}
        pagination={false}
      />

      <Modal
        visible={scheduleVisible}
        onCancel={() => setScheduleVisible(false)}
        title={editingSchedule ? t['schedule.edit'] : t['schedule.new']}
        onOk={handleScheduleSubmit}
      >
        <Form form={scheduleForm} layout="vertical">
          <Form.Item field="name" label={t['schedule.name']} rules={[{ required: true }]}>
            <Input placeholder={t['schedule.name.placeholder']} />
          </Form.Item>
          <Form.Item field="cron_expression" label={t['schedule.cron']} rules={[{ required: true }]}>
            <Input placeholder={t['schedule.cron.placeholder']} />
          </Form.Item>
          <Form.Item field="task_type" label={t['schedule.task_type']} rules={[{ required: true }]}>
            <Select
              options={[
                { label: t['schedule.task_type.testcase'], value: 'testcase' },
                { label: t['schedule.task_type.module'], value: 'module' },
                { label: t['schedule.task_type.project'], value: 'project' },
              ]}
            />
          </Form.Item>
          <Form.Item field="target_id" label={t['schedule.target_id']} rules={[{ required: true, type: 'number' }]}>
            <Input type="number" placeholder={t['schedule.target_id.placeholder']} />
          </Form.Item>
          <Form.Item field="description" label={t['description']}>
            <Input.TextArea placeholder={t['description']} />
          </Form.Item>
          <Form.Item field="enabled" label={t['schedule.enabled']} triggerPropName="checked">
            <Switch />
          </Form.Item>
        </Form>
      </Modal>
    </Card>
  );
}

export default ScheduleManagement;
