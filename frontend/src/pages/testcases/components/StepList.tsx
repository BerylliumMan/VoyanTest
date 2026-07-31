import React from 'react';
import { Button, Tag, Input, Typography, Space, Switch, Select } from '@arco-design/web-react';
import { IconMenu, IconPlus, IconCopy, IconDelete, IconTool } from '@arco-design/web-react/icon';
import { CaseKind, Step } from '../types';
import styles from '../style/components.module.less';

const UI_ACTIONS = [
  { value: 'goto', label: '打开/进入' },
  { value: 'click', label: '点击' },
  { value: 'fill', label: '输入' },
  { value: 'select', label: '选择' },
  { value: 'check', label: '勾选' },
  { value: 'uncheck', label: '取消勾选' },
  { value: 'wait', label: '等待文案' },
  { value: 'assert_text', label: '断言包含' },
  { value: 'assert_visible', label: '断言可见' },
  { value: 'hover', label: '悬停' },
  { value: 'press_key', label: '按键' },
  { value: 'click_blank', label: '点击空白' },
  { value: 'icon_click', label: '图标点击' },
];

function renderStructuredDescription(s: Record<string, unknown> | null | undefined): string {
  if (!s || !s.action) return '';
  const action = String(s.action);
  const name = String(s.target_name || '');
  const value = s.value == null ? '' : String(s.value);
  const icon = String(s.icon_hint || '');
  const dis = String(s.disambiguation || '');
  const suffix = dis ? `（${dis}）` : '';
  switch (action) {
    case 'goto':
      return name || value ? `打开【${name || value}】` : '打开页面';
    case 'click':
      return name ? `点击【${name}】${suffix}` : '点击';
    case 'fill':
      return name ? `在【${name}】输入 ${value}`.trim() : `输入 ${value}`.trim();
    case 'select':
      if (name && value) return `在【${name}】中选择【${value}】`;
      return `选择【${value || name}】`;
    case 'check':
      return name ? `勾选【${name}】` : '勾选';
    case 'uncheck':
      return name ? `取消勾选【${name}】` : '取消勾选';
    case 'wait':
      return (value || name) ? `等待【${value || name}】出现` : '等待页面稳定';
    case 'assert_text':
      return `断言页面包含【${value || name}】`;
    case 'assert_visible':
      return name ? `断言【${name}】可见` : '断言元素可见';
    case 'hover':
      return name ? `悬停【${name}】` : '悬停';
    case 'press_key':
      return `按键 ${value || name}`.trim();
    case 'click_blank':
      return '点击空白处';
    case 'icon_click':
      return icon ? (icon.startsWith('点击') ? icon : `点击${icon}`) : (name ? `点击【${name}】` : '点击图标');
    default:
      return name ? `${action}【${name}】` : action;
  }
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

  const patchStructured = (idx: number, step: Step, patch: Record<string, unknown>) => {
    const next = { ...(step.structured_step || {}), ...patch };
    if (!next.action) next.action = 'click';
    const desc = renderStructuredDescription(next);
    onUpdate(idx, 'structured_step', next);
    if (desc) onUpdate(idx, 'description', desc);
  };

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
            {isUi ? (
              <div className={styles['step-row-fields']} style={{ flexWrap: 'wrap', gap: 8 }}>
                <Select
                  style={{ width: 120 }}
                  value={(step.structured_step?.action as string) || 'click'}
                  options={UI_ACTIONS}
                  onChange={(v) => patchStructured(idx, step, { action: v })}
                />
                <Input
                  style={{ flex: 1, minWidth: 120 }}
                  placeholder="目标文案 target_name"
                  value={(step.structured_step?.target_name as string) || ''}
                  onChange={(v) => patchStructured(idx, step, { target_name: v })}
                />
                <Input
                  style={{ flex: 1, minWidth: 120 }}
                  placeholder="值 value（输入/选项/等待）"
                  value={step.structured_step?.value == null ? '' : String(step.structured_step.value)}
                  onChange={(v) => patchStructured(idx, step, { value: v })}
                />
                {(step.structured_step?.action === 'icon_click') && (
                  <Input
                    style={{ flex: 2, minWidth: 200 }}
                    placeholder="图标描述 icon_hint（位置+外观+用途）"
                    value={(step.structured_step?.icon_hint as string) || ''}
                    onChange={(v) => patchStructured(idx, step, { icon_hint: v })}
                  />
                )}
                <Input.TextArea
                  className={styles['step-input']}
                  placeholder={t['step.result.placeholder'] || '预期结果'}
                  value={step.parsed_result || ''}
                  onChange={(v) => onUpdate(idx, 'parsed_result', v)}
                  autoSize={{ minRows: 1 }}
                />
                <Typography.Text type="secondary" style={{ width: '100%', fontSize: 12 }}>
                  预览：{step.description || renderStructuredDescription(step.structured_step) || '（完善动作/目标后自动生成）'}
                </Typography.Text>
              </div>
            ) : (
              <div className={styles['step-row-fields']}>
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
                />
              </div>
            )}
            {step.healed_selector && (
              <div className={styles['healed-hint']}>
                <IconTool /> 已修复: {step.healed_selector}
              </div>
            )}
            {isUi && step.learned_locator && (
              <div className={styles['healed-hint']}>
                <IconTool /> 已记忆定位: {[
                  (step.learned_locator as any).role,
                  (step.learned_locator as any).name,
                  Array.isArray((step.learned_locator as any).plan)
                    ? `plan×${(step.learned_locator as any).plan.length}`
                    : null,
                ].filter(Boolean).join(' / ') || '已缓存'}
                <Button
                  type="text"
                  size="mini"
                  status="warning"
                  onClick={() => onUpdate(idx, 'learned_locator', null as any)}
                  style={{ marginLeft: 8 }}
                >
                  清除
                </Button>
              </div>
            )}
            {isUi && (
              <div className={styles['healed-hint']} style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                <Switch
                  size="small"
                  checked={step.cacheable !== false}
                  onChange={(v) => onUpdate(idx, 'cacheable', v)}
                />
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                  可缓存定位
                </Typography.Text>
              </div>
            )}
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
