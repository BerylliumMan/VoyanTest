import React, { useState, useEffect } from 'react';
import {
  Modal,
  Form,
  Select,
  Input,
  Message,
} from '@arco-design/web-react';
import { apiGet, apiPost } from '@/utils/apiRequest';

interface SaveAsCaseDialogProps {
  visible: boolean;
  onClose: () => void;
  steps: { step_description: string; expected_result: string }[];
  onSaved: (caseId: number) => void;
}

const SaveAsCaseDialog: React.FC<SaveAsCaseDialogProps> = ({
  visible, onClose, steps, onSaved,
}) => {
  const [form] = Form.useForm();
  const [projects, setProjects] = useState<{ id: number; name: string }[]>([]);
  const [modules, setModules] = useState<{ id: number; name: string }[]>([]);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (visible) {
      apiGet<{ id: number; name: string }[]>('/api/projects/')
        .then(setProjects)
        .catch(() => Message.error('加载项目列表失败'));
      form.resetFields();
    }
  }, [visible]);

  const handleProjectChange = (pid: number) => {
    form.setFieldValue('module_id', undefined);
    setModules([]);
    if (pid) {
      apiGet<{ id: number; name: string }[]>(`/api/projects/${pid}/modules/tree`)
        .then(setModules)
        .catch(() => {});
    }
  };

  const handleOk = async () => {
    try {
      const values = await form.validate();
      setSaving(true);
      const data = await apiPost<{ case_id: number; name: string; steps_count: number }>(
        '/api/recordings/save-as-case',
        {
          project_id: values.project_id,
          module_id: values.module_id || null,
          name: values.name,
          steps: steps.map((s) => ({
            step_description: s.step_description,
            expected_result: s.expected_result,
          })),
        },
      );
      Message.success(`已保存为用例「${data.name}」(${data.steps_count} 步)`);
      onSaved(data.case_id);
    } catch (e: any) {
      if (e?.errors) return; // form validation error
      Message.error('保存失败: ' + (e?.message || '未知错误'));
    } finally {
      setSaving(false);
    }
  };

  return (
    <Modal
      title="保存为测试用例"
      visible={visible}
      onOk={handleOk}
      onCancel={onClose}
      confirmLoading={saving}
      okText="保存"
      cancelText="取消"
    >
      <Form form={form} layout="vertical">
        <Form.Item
          label="项目"
          field="project_id"
          rules={[{ required: true, message: '请选择项目' }]}
        >
          <Select
            placeholder="选择项目"
            onChange={handleProjectChange}
          >
            {projects.map((p) => (
              <Select.Option key={p.id} value={p.id}>{p.name}</Select.Option>
            ))}
          </Select>
        </Form.Item>
        <Form.Item label="模块" field="module_id">
          <Select placeholder="选择模块（可选）" allowClear>
            {modules.map((m) => (
              <Select.Option key={m.id} value={m.id}>{m.name}</Select.Option>
            ))}
          </Select>
        </Form.Item>
        <Form.Item
          label="用例名称"
          field="name"
          rules={[{ required: true, message: '请输入用例名称' }]}
        >
          <Input placeholder="输入测试用例名称" />
        </Form.Item>
      </Form>
    </Modal>
  );
};

export default SaveAsCaseDialog;
