import React from 'react';
import { Button, Input, Switch, Space } from '@arco-design/web-react';
import { IconDragDotVertical, IconPlus, IconCopy, IconDelete, IconTool } from '@arco-design/web-react/icon';
import { CaseKind, Step } from '../types';
import { deriveStructuredFromDescription } from '../utils/parseInstantStep';
import styles from '../style/components.module.less';

function locatorSummary(loc: Record<string, unknown>): string {
  const plan = Array.isArray(loc.plan) ? `plan×${loc.plan.length}` : null;
  return [loc.role, loc.name, plan].filter(Boolean).join(' / ') || '已缓存';
}

interface StepListProps {
  steps: Step[];
  onAdd: () => void;
  onRemove: (idx: number) => void;
  onUpdate: (idx: number, field: string, value: string | number | boolean | null | Record<string, unknown>) => void;
  onInsert: (idx: number) => void;
  onCopy: (idx: number) => void;
  onPaste: (idx: number) => void;
  copiedStep: Step | null;
  onDragStart: (idx: number) => (e: React.DragEvent) => void;
  onDragOver: (idx: number) => (e: React.DragEvent) => void;
  onDragLeave: (idx: number) => (e: React.DragEvent) => void;
  onDrop: (targetIdx: number) => (e: React.DragEvent) => void;
  t: Record<string, string>;
  caseKind?: CaseKind;
}

const StepList: React.FC<StepListProps> = ({
  steps, onAdd, onRemove, onUpdate, onInsert, onCopy, onPaste, copiedStep,
  onDragStart, onDragOver, onDragLeave, onDrop, t, caseKind = 'ui',
}) => {
  const isUi = caseKind === 'ui';

  /**
   * 描述优先编辑：改写描述时静默重新派生结构化提示（不向用户暴露动作概念）。
   * 解析不出则置空，交由模型按描述执行；动作/目标一致时保留固化 selector。
   */
  const handleDescriptionChange = (idx: number, step: Step, text: string) => {
    const derived = deriveStructuredFromDescription(
      text,
      (step.structured_step || null) as Record<string, unknown> | null,
    );
    onUpdate(idx, '_patch', { description: text, structured_step: derived });
  };

  const clearSelector = (idx: number, step: Step) => {
    const next = { ...(step.structured_step || {}) };
    delete next.selector;
    onUpdate(idx, '_patch', { structured_step: next });
  };

  return (
    <div className={styles['step-list']}>
      {steps.map((step, idx) => {
        return (
          <div
            key={idx}
            className={`step-row ${styles['step-card']}`}
            onDragOver={onDragOver(idx)}
            onDragLeave={onDragLeave(idx)}
            onDrop={onDrop(idx)}
          >
            <div className={styles['step-card-head']}>
              <div className={styles['step-card-head-left']}>
                <button
                  type="button"
                  className={styles['drag-handle']}
                  draggable
                  onDragStart={onDragStart(idx)}
                  aria-label="拖拽排序"
                  title="拖拽排序"
                >
                  <IconDragDotVertical />
                </button>
                <span className={styles['step-index']}>{idx + 1}</span>
              </div>
              <Space size={0} className={styles['step-card-actions']}>
                <Button type="text" size="mini" icon={<IconPlus />} onClick={() => onInsert(idx)} title={t['step.insert_above']} aria-label="插入步骤" />
                <Button type="text" size="mini" icon={<IconCopy />} onClick={() => onCopy(idx)} title={t['step.copy']} aria-label="复制步骤" />
                {copiedStep && (
                  <Button type="text" size="mini" icon={<IconPlus />} onClick={() => onPaste(idx)} title={t['step.paste']} aria-label="粘贴步骤" />
                )}
                <Button type="text" size="mini" status="danger" icon={<IconDelete />} onClick={() => onRemove(idx)} aria-label="删除步骤" />
              </Space>
            </div>

            <div className={styles['step-card-body']}>
              {isUi ? (
                <>
                  <div className={styles['step-grid-functional']}>
                    <div className={`${styles['step-field']} ${styles['step-field-full']}`}>
                      <label className={styles['step-label']}>步骤描述</label>
                      <Input.TextArea
                        placeholder={t['step.placeholder'] || '用自然语言描述测试步骤，例如：在【Username】输入 standard_user'}
                        value={step.description}
                        onChange={(v) => handleDescriptionChange(idx, step, v)}
                        autoSize={{ minRows: 1, maxRows: 4 }}
                      />
                    </div>
                    <div className={`${styles['step-field']} ${styles['step-field-full']}`}>
                      <label className={styles['step-label']}>预期结果</label>
                      <Input
                        size="small"
                        placeholder={t['step.result.placeholder'] || '可选，步骤成功后的可见变化'}
                        value={step.parsed_result || ''}
                        onChange={(v) => onUpdate(idx, 'parsed_result', v)}
                      />
                    </div>
                  </div>

                  <div className={styles['step-meta']}>
                    <div className={styles['step-meta-left']}>
                      {!!(step.structured_step?.selector) && (
                        <span
                          className={styles['step-meta-item']}
                          title={String(step.structured_step?.selector)}
                        >
                          <IconTool /> 已固化 {String(step.structured_step?.selector)}
                          <Button
                            type="text"
                            size="mini"
                            status="warning"
                            className={styles['step-meta-clear']}
                            onClick={() => clearSelector(idx, step)}
                          >
                            清除
                          </Button>
                        </span>
                      )}
                      {step.healed_selector && (
                        <span className={styles['step-meta-item']}>
                          <IconTool /> 已修复 {step.healed_selector}
                        </span>
                      )}
                      {step.learned_locator && (
                        <span className={styles['step-meta-item']}>
                          <IconTool /> 已记忆 {locatorSummary(step.learned_locator as Record<string, unknown>)}
                          <Button
                            type="text"
                            size="mini"
                            status="warning"
                            className={styles['step-meta-clear']}
                            onClick={() => onUpdate(idx, 'learned_locator', null as any)}
                          >
                            清除
                          </Button>
                        </span>
                      )}
                    </div>
                    <label className={styles['step-cache']}>
                      <Switch
                        size="small"
                        checked={step.cacheable !== false}
                        onChange={(v) => onUpdate(idx, 'cacheable', v)}
                      />
                      <span>可缓存定位</span>
                    </label>
                  </div>
                </>
              ) : (
                <div className={styles['step-grid-functional']}>
                  <div className={`${styles['step-field']} ${styles['step-field-full']}`}>
                    <label className={styles['step-label']}>步骤描述</label>
                    <Input.TextArea
                      placeholder={t['step.placeholder']}
                      value={step.description}
                      onChange={(v) => onUpdate(idx, 'description', v)}
                      autoSize={{ minRows: 1, maxRows: 4 }}
                    />
                  </div>
                  <div className={`${styles['step-field']} ${styles['step-field-full']}`}>
                    <label className={styles['step-label']}>预期结果</label>
                    <Input.TextArea
                      placeholder={t['step.result.placeholder'] || '预期结果'}
                      value={step.parsed_result || ''}
                      onChange={(v) => onUpdate(idx, 'parsed_result', v)}
                      autoSize={{ minRows: 1, maxRows: 3 }}
                    />
                  </div>
                </div>
              )}
            </div>
          </div>
        );
      })}
      <Button type="dashed" long className={styles['step-add']} onClick={onAdd}>
        {t['add.step']}
      </Button>
    </div>
  );
};

export default StepList;
