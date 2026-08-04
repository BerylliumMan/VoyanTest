import React, { useState } from 'react';
import {
  Modal, Form, Input, Select, Message, Tag, Button, Space,
} from '@arco-design/web-react';
import axios from 'axios';
import { Step, Module, TestCase, CaseKind } from '../types';
import StepList from './StepList';
import styles from '../style/components.module.less';

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
  caseKind?: CaseKind;
  onCompiledScriptCleared?: () => void;
}

const TestCaseEditor: React.FC<TestCaseEditorProps> = ({
  visible, editingCase, onCancel, onSubmit, modules, projectId, t, form, steps, setSteps, caseKind = 'ui',
  onCompiledScriptCleared,
}) => {
  const [copiedStep, setCopiedStep] = useState<Step | null>(null);
  const [scriptModalVisible, setScriptModalVisible] = useState(false);
  const [clearingScript, setClearingScript] = useState(false);
  const kindLabel = caseKind === 'functional'
    ? (t['menu.testcases.functional'] || '功能测试用例')
    : (t['menu.testcases.ui'] || 'UI自动化用例');

  const hasCompiledScript = Boolean(editingCase?.compiled_script);

  const handleClearCompiledScript = async () => {
    if (!editingCase?.id) return;
    setClearingScript(true);
    try {
      await axios.delete(`/api/testcases/${editingCase.id}/compiled-script`);
      Message.success(t['compiled_script.cleared'] || '已清除固化脚本');
      onCompiledScriptCleared?.();
    } catch {
      Message.error(t['compiled_script.clear_failed'] || '清除固化脚本失败');
    } finally {
      setClearingScript(false);
    }
  };

  const addStep = () => setSteps([...steps, {
    step_order: steps.length + 1,
    description: '',
    parsed_result: '',
    retry_max: 0,
    retry_delay: 1.0,
    cacheable: true,
    structured_step: caseKind === 'ui' ? { action: 'click' } : null,
  }]);
  const removeStep = (idx: number) => setSteps(steps.filter((_, i) => i !== idx).map((s, i) => ({ ...s, step_order: i + 1 })));
  const updateStep = (idx: number, field: string, value: string | number | boolean | null | Record<string, unknown>) => {
    const newSteps = [...steps];
    if (field === '_patch' && value && typeof value === 'object' && !Array.isArray(value)) {
      newSteps[idx] = { ...newSteps[idx], ...(value as Record<string, unknown>) } as typeof newSteps[number];
    } else {
      newSteps[idx] = { ...newSteps[idx], [field]: value };
    }
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
      onOk={onSubmit} className={styles['editor-modal']}
    >
      <Form form={form} layout="vertical">
        <Form.Item label={t['case.kind'] || '用例类型'}>
          <div className={styles['editor-kind-row']}>
            <Tag color={caseKind === 'functional' ? 'arcoblue' : 'green'}>{kindLabel}</Tag>
          </div>
        </Form.Item>
        {caseKind === 'ui' && hasCompiledScript ? (
          <div
            style={{
              marginBottom: 16,
              padding: '10px 12px',
              background: 'var(--color-fill-2)',
              borderRadius: 6,
              fontSize: 13,
            }}
          >
            <div style={{ marginBottom: 8 }}>
              {t['compiled_script.hint']
                || '已固化为 Playwright 脚本：下次优先回放；修改步骤并保存后将自动清除。可由自然语言目标跑通后自动合成。'}
              {editingCase?.compiled_at ? (
                <span style={{ color: 'var(--color-text-3)', marginLeft: 8 }}>
                  {String(editingCase.compiled_at)}
                </span>
              ) : null}
            </div>
            <Space>
              <Button size="mini" type="outline" onClick={() => setScriptModalVisible(true)}>
                {t['compiled_script.view'] || '查看脚本'}
              </Button>
              <Button
                size="mini"
                status="warning"
                loading={clearingScript}
                onClick={handleClearCompiledScript}
              >
                {t['compiled_script.clear'] || '清除脚本'}
              </Button>
            </Space>
          </div>
        ) : null}
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
            caseKind={caseKind}
          />
        </Form.Item>
      </Form>
      <Modal
        visible={scriptModalVisible}
        onCancel={() => setScriptModalVisible(false)}
        title={t['compiled_script.view'] || '查看固化脚本'}
        footer={null}
        style={{ width: 720 }}
      >
        <Input.TextArea
          value={editingCase?.compiled_script || ''}
          readOnly
          autoSize={{ minRows: 16, maxRows: 28 }}
          style={{ fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace', fontSize: 12 }}
        />
      </Modal>
    </Modal>
  );
};

export default TestCaseEditor;
