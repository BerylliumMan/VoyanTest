import React from 'react';
import { Button, Tag, Input } from '@arco-design/web-react';
import { IconMenu, IconPlus, IconCopy, IconDelete } from '@arco-design/web-react/icon';
import { Step } from '../types';
import styles from '../style/components.module.less';

interface StepListProps {
  steps: Step[];
  onAdd: () => void;
  onRemove: (idx: number) => void;
  onUpdate: (idx: number, field: string, value: string) => void;
  onInsert: (idx: number) => void;
  onCopy: (idx: number) => void;
  onPaste: (idx: number) => void;
  copiedStep: Step | null;
  onDragStart: (idx: number) => (e: React.DragEvent) => void;
  onDragOver: (idx: number) => (e: React.DragEvent) => void;
  onDragLeave: (idx: number) => (e: React.DragEvent) => void;
  onDrop: (targetIdx: number) => (e: React.DragEvent) => void;
  t: Record<string, string>;
}

const StepList: React.FC<StepListProps> = ({
  steps, onAdd, onRemove, onUpdate, onInsert, onCopy, onPaste, copiedStep,
  onDragStart, onDragOver, onDragLeave, onDrop, t,
}) => {
  return (
    <div>
      {steps.map((step, idx) => (
        <div key={idx} className={`step-row ${styles['step-row']}`}
          onDragOver={onDragOver(idx)}
          onDragLeave={onDragLeave(idx)}
          onDrop={onDrop(idx)}
        >
          <Button type="text" icon={<IconMenu />} aria-label="拖拽排序"
            draggable
            onDragStart={onDragStart(idx)}
            className={styles['drag-handle']}
          />
          <Tag className={styles['step-number-tag']}>{idx + 1}</Tag>
          <Input.TextArea
            className={styles['step-input']}
            placeholder={t['step.placeholder']}
            value={step.description}
            onChange={(v) => onUpdate(idx, 'description', v)}
            autoSize={{ minRows: 1 }}
          />
          <Input.TextArea
            className={styles['step-input']}
            placeholder={t['step.result.placeholder'] || '预期结果'}
            value={step.parsed_result || ''}
            onChange={(v) => onUpdate(idx, 'parsed_result', v)}
            autoSize={{ minRows: 1 }}
            style={{ flex: 1 }}
          />
          <Button type="text" icon={<IconPlus />} onClick={() => onInsert(idx)} title={t['step.insert_above']} aria-label="插入步骤" />
          <Button type="text" icon={<IconCopy />} onClick={() => onCopy(idx)} title={t['step.copy']} aria-label="复制步骤" />
          {copiedStep && (
            <Button type="text" icon={<IconPlus />} onClick={() => onPaste(idx)} title={t['step.paste']} aria-label="粘贴步骤" />
          )}
          <Button type="text" status="danger" icon={<IconDelete />} onClick={() => onRemove(idx)} aria-label="删除步骤" />
        </div>
      ))}
      <Button type="dashed" long onClick={onAdd}>{t['add.step']}</Button>
    </div>
  );
};

export default StepList;
