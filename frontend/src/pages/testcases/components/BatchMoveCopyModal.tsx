import React from 'react';
import { Modal, Form, Select } from '@arco-design/web-react';
import { Module, Project } from '../types';

interface BatchMoveCopyModalProps {
  visible: boolean;
  batchAction: 'move' | 'copy';
  onCancel: () => void;
  onSubmit: () => void;
  projects: Project[];
  targetProjectId: number | null;
  onTargetProjectChange: (val: number) => void;
  targetModuleId: number | null;
  onTargetModuleChange: (val: number | null) => void;
  targetModules: Module[];
  submitting: boolean;
  t: Record<string, string>;
}

const BatchMoveCopyModal: React.FC<BatchMoveCopyModalProps> = ({
  visible, batchAction, onCancel, onSubmit, projects,
  targetProjectId, onTargetProjectChange,
  targetModuleId, onTargetModuleChange, targetModules,
  submitting, t,
}) => {
  return (
    <Modal
      visible={visible} onCancel={onCancel}
      title={batchAction === 'move' ? t['batch.move.title'] : t['batch.copy.title']}
      onOk={onSubmit}
      confirmLoading={submitting}
      okText={batchAction === 'move' ? t['move'] : t['copy']}
    >
      <Form layout="vertical">
        <Form.Item label={t['target.project']} rules={[{ required: true }]}>
          <Select
            placeholder={t['select.project']}
            value={targetProjectId ?? undefined}
            onChange={(val) => onTargetProjectChange(val as number)}
            options={projects.map((p) => ({ label: p.name, value: p.id }))}
            showSearch
            className="testcase-select"
          />
        </Form.Item>
        <Form.Item label={t['target.module']}>
          <Select
            placeholder={t['root.module']}
            value={targetModuleId ?? undefined}
            onChange={(val) =>
              onTargetModuleChange(typeof val === 'number' ? val : null)
            }
            options={targetModules.map((m) => ({ label: m.name, value: m.id }))}
            allowClear
            className="testcase-select"
          />
        </Form.Item>
      </Form>
    </Modal>
  );
};

export default BatchMoveCopyModal;
