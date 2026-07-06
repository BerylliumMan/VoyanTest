import React from 'react';
import { Modal, Form, Input, Select } from '@arco-design/web-react';
import { Module } from '../types';

type FormInstance = ReturnType<typeof Form.useForm>[0];

interface ModuleEditorProps {
  visible: boolean;
  editingModule: Module | null;
  onCancel: () => void;
  onSubmit: () => void;
  modules: Module[];
  form: FormInstance;
  t: Record<string, string>;
}

const ModuleEditor: React.FC<ModuleEditorProps> = ({
  visible, editingModule, onCancel, onSubmit, modules, form, t,
}) => {
  return (
    <Modal visible={visible} onCancel={onCancel}
      title={editingModule ? t['edit'] : t['module.name']} onOk={onSubmit}
    >
      <Form form={form} layout="vertical">
        <Form.Item field="name" label={t['module.name']} rules={[{ required: true }]}>
          <Input placeholder={t['module.name']} />
        </Form.Item>
        <Form.Item field="parent_id" label={t['parent.module']}>
          <Select placeholder={t['root.module']} allowClear className="testcase-select"
            options={modules.filter(m => m.id !== editingModule?.id).map(m => ({ label: m.name, value: m.id }))}
          />
        </Form.Item>
        <Form.Item field="description" label={t['description']}>
          <Input.TextArea placeholder={t['description']} rows={3} />
        </Form.Item>
      </Form>
    </Modal>
  );
};

export default ModuleEditor;
