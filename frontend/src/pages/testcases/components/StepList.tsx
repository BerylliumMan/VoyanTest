import React, { useState } from 'react';
import { Button, Tag, Input, InputNumber, Collapse, Typography } from '@arco-design/web-react';
import { IconMenu, IconPlus, IconCopy, IconDelete, IconSettings } from '@arco-design/web-react/icon';
import { Step } from '../types';
import styles from '../style/components.module.less';

interface StepListProps {
  steps: Step[];
  onAdd: () => void;
  onRemove: (idx: number) => void;
  onUpdate: (idx: number, field: string, value: string | number) => void;
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

const CollapseItem = Collapse.Item;

const StepList: React.FC<StepListProps> = ({
  steps, onAdd, onRemove, onUpdate, onInsert, onCopy, onPaste, copiedStep,
  onDragStart, onDragOver, onDragLeave, onDrop, t,
}) => {
  // 追踪每个步骤的高级设置展开状态
  const [expandedKeys, setExpandedKeys] = useState<string[]>([]);

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
          <div className={styles['step-fields']}>
            <Input.TextArea
              className={styles['step-input']}
              placeholder={t['step.placeholder']}
              value={step.description}
              onChange={(v) => onUpdate(idx, 'description', v)}
              autoSize={{ minRows: 1 }}
            />
            {step.healed_selector && (
              <Typography.Text
                type="secondary"
                style={{ fontSize: 12, color: 'var(--color-text-3)', display: 'block', marginTop: 2 }}
              >
                🔧 已修复: {step.healed_selector}
              </Typography.Text>
            )}
            <Input.TextArea
              className={styles['step-input']}
              placeholder={t['step.result.placeholder'] || '预期结果'}
              value={step.parsed_result || ''}
              onChange={(v) => onUpdate(idx, 'parsed_result', v)}
              autoSize={{ minRows: 1 }}
            />
            {/* 高级设置折叠面板 */}
            <Collapse
              className={styles['retry-collapse']}
              activeKey={expandedKeys}
              onChange={(keys) => setExpandedKeys(Array.isArray(keys) ? keys : keys ? [keys] : [])}
              expandIcon={<IconSettings />}
            >
              <CollapseItem
                key={`retry-${idx}`}
                name={`retry-${idx}`}
                header="高级设置"
                showExpandIcon={true}
              >
                <div className={styles['retry-fields']}>
                  <div className={styles['retry-field']}>
                    <span className={styles['retry-label']}>失败重试次数</span>
                    <InputNumber
                      value={step.retry_max ?? 0}
                      min={0}
                      max={10}
                      step={1}
                      precision={0}
                      placeholder="失败时最多重试次数，0表示不重试"
                      onChange={(v) => onUpdate(idx, 'retry_max', v ?? 0)}
                      style={{ width: 180 }}
                    />
                  </div>
                  <div className={styles['retry-field']}>
                    <span className={styles['retry-label']}>重试间隔(秒)</span>
                    <InputNumber
                      value={step.retry_delay ?? 1.0}
                      min={0.1}
                      max={300}
                      step={0.1}
                      precision={1}
                      placeholder="每次重试之间的等待时间"
                      onChange={(v) => onUpdate(idx, 'retry_delay', v ?? 1.0)}
                      style={{ width: 180 }}
                    />
                  </div>
                </div>
              </CollapseItem>
            </Collapse>
          </div>
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
