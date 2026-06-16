import React, { useState } from 'react';
import {
  Modal, Form, Input, Select, Message, Collapse,
} from '@arco-design/web-react';
import { Step, Module, TestCase } from '../types';
import StepList from './StepList';

interface StepItem { step_order: number; description: string; }
interface TestCaseDetail extends TestCase { steps: StepItem[]; }

type FormInstance = ReturnType<typeof Form.useForm>[0];

interface TestCaseEditorProps {
  visible: boolean;
  editingCase: TestCaseDetail | null;
  onCancel: () => void;
  onSubmit: () => void;
  modules: Module[];
  projectId: number | null;
  t: Record<string, string>;
  form: FormInstance;
  steps: Step[];
  setSteps: React.Dispatch<React.SetStateAction<Step[]>>;
}

const TestCaseEditor: React.FC<TestCaseEditorProps> = ({
  visible, editingCase, onCancel, onSubmit, modules, projectId, t, form, steps, setSteps,
}) => {
  const [copiedStep, setCopiedStep] = useState<Step | null>(null);

  const addStep = () => setSteps([...steps, { step_order: steps.length + 1, description: '', parsed_result: '', retry_max: 0, retry_delay: 1.0 }]);
  const removeStep = (idx: number) => setSteps(steps.filter((_, i) => i !== idx).map((s, i) => ({ ...s, step_order: i + 1 })));
  const updateStep = (idx: number, field: string, value: string | number) => {
    const newSteps = [...steps];
    newSteps[idx] = { ...newSteps[idx], [field]: value };
    setSteps(newSteps);
  };

  const handleDragStart = (idx: number) => (e: React.DragEvent) => {
    e.stopPropagation();
    e.dataTransfer.effectAllowed = 'move';
    e.dataTransfer.setData('text/plain', String(idx));
  };
  const handleDragOver = (idx: number) => (e: React.DragEvent) => {
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';
    e.currentTarget.classList.add('drag-over');
  };
  const handleDragLeave = (idx: number) => (e: React.DragEvent) => {
    e.currentTarget.classList.remove('drag-over');
  };
  const handleDrop = (targetIdx: number) => (e: React.DragEvent) => {
    e.preventDefault();
    e.currentTarget.classList.remove('drag-over');
    const sourceIdx = parseInt(e.dataTransfer.getData('text/plain'));
    if (isNaN(sourceIdx) || sourceIdx === targetIdx) return;
    const newSteps = [...steps];
    const [moved] = newSteps.splice(sourceIdx, 1);
    newSteps.splice(targetIdx, 0, moved);
    setSteps(newSteps.map((s, i) => ({ ...s, step_order: i + 1 })));
  };

  const insertStep = (idx: number) => {
    const newSteps = [...steps];
    newSteps.splice(idx, 0, { step_order: idx + 1, description: '', parsed_result: '', retry_max: 0, retry_delay: 1.0 });
    setSteps(newSteps.map((s, i) => ({ ...s, step_order: i + 1 })));
  };
  const copyStep = (idx: number) => {
    setCopiedStep(steps[idx]);
    Message.success(t['step.copied']);
  };
  const pasteStep = (idx: number) => {
    if (!copiedStep) return;
    const newSteps = [...steps];
    newSteps.splice(idx + 1, 0, { ...copiedStep, step_order: idx + 2 });
    setSteps(newSteps.map((s, i) => ({ ...s, step_order: i + 1 })));
  };

  return (
    <Modal
      visible={visible} onCancel={onCancel}
      title={editingCase ? t['edit.case'] : t['new.case']}
      onOk={onSubmit} style={{ width: 700 }}
    >
      <Form form={form} layout="vertical">
        <Form.Item field="name" label={t['name']} rules={[{ required: true, message: t['case.name.placeholder'] }]}>
          <Input placeholder={t['case.name.placeholder']} />
        </Form.Item>
        <Form.Item field="module_id" label={t['module']} rules={[{ required: true, message: t['select.module'] }]}>
          <Select placeholder={t['select.module']} className="testcase-select"
            options={modules.map((m) => ({ label: m.name, value: m.id }))}
          />
        </Form.Item>
        <Form.Item field="description" label={t['description']}>
          <Input.TextArea
            placeholder={t['description']}
            autoSize={{ minRows: 2 }}
          />
        </Form.Item>
        <Form.Item label={t['case.steps']}>
          <StepList
            steps={steps}
            onAdd={addStep}
            onRemove={removeStep}
            onUpdate={updateStep}
            onInsert={insertStep}
            onCopy={copyStep}
            onPaste={pasteStep}
            copiedStep={copiedStep}
            onDragStart={handleDragStart}
            onDragOver={handleDragOver}
            onDragLeave={handleDragLeave}
            onDrop={handleDrop}
            t={t}
          />
        </Form.Item>
      </Form>
    </Modal>
  );
};

export default TestCaseEditor;
