import React from 'react';
import { Button, Input, Typography, Switch, Select, Space } from '@arco-design/web-react';
import { IconDragDotVertical, IconPlus, IconCopy, IconDelete, IconTool } from '@arco-design/web-react/icon';
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
  const isContextName =
    /(?:搜索结果|筛选结果|下拉|列表|弹窗|对话框|菜单|树|表格|选项).{0,6}中的?$/.test(name) ||
    /(?:中的|里的)$/.test(name);
  switch (action) {
    case 'goto':
      return name || value ? `打开【${name || value}】` : '打开页面';
    case 'click': {
      let label = name;
      let ctx = dis;
      if (value && isContextName) {
        label = value;
        ctx = dis || name;
      } else if (value && !name) {
        label = value;
      }
      const note = String(s.note || '');
      if (
        label === '关闭' &&
        (ctx.includes('所有对话框') || note.includes('对话框') || icon.includes('【X】'))
      ) {
        const parts = ['等待页面中间出现对话框'];
        if (ctx.includes('所有') || note.includes('全部') || note.includes('所有')) {
          parts.push('把所有对话框都点击【关闭】按钮');
        } else {
          parts.push('点击【关闭】按钮');
        }
        if (icon || note.includes('【X】')) {
          parts.push('或【X】形状的关闭标志');
        }
        return parts.join('，');
      }
      const suf = ctx ? `（${ctx}）` : suffix;
      const extra = icon ? `；备选：${icon}` : '';
      return label ? `点击【${label}】${suf}${extra}` : suf ? `点击${suf}` : '点击';
    }
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

  const patchStructured = (idx: number, step: Step, patch: Record<string, unknown>) => {
    const next = { ...(step.structured_step || {}), ...patch };
    if (!next.action) next.action = 'click';
    const desc = renderStructuredDescription(next);
    onUpdate(idx, '_patch', { structured_step: next, description: desc || step.description || '' });
  };

  return (
    <div className={styles['step-list']}>
      {steps.map((step, idx) => {
        const preview =
          step.description ||
          renderStructuredDescription(step.structured_step) ||
          '完善动作与目标后自动生成预览';
        const action = (step.structured_step?.action as string) || 'click';
        const showValue = ['fill', 'select', 'wait', 'assert_text', 'press_key', 'goto'].includes(action);
        const showDisambiguation = ['click', 'fill', 'select', 'hover', 'check', 'uncheck'].includes(action);
        const showIconHint =
          action === 'icon_click' ||
          !!(step.structured_step?.icon_hint) ||
          action === 'click';
        const showNote = !!(step.structured_step?.note) || action === 'wait';

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
                {isUi && (
                  <Typography.Text className={styles['step-preview-inline']} ellipsis={{ showTooltip: true }}>
                    {preview}
                  </Typography.Text>
                )}
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
                  <div className={styles['step-grid']}>
                    <div className={styles['step-field']}>
                      <label className={styles['step-label']}>动作</label>
                      <Select
                        size="small"
                        value={action}
                        options={UI_ACTIONS}
                        onChange={(v) => patchStructured(idx, step, { action: v })}
                      />
                    </div>
                    <div className={`${styles['step-field']} ${styles['step-field-wide']}`}>
                      <label className={styles['step-label']}>目标</label>
                      <Input
                        size="small"
                        placeholder="页面可见文案"
                        title="目标文案：页面上可见的按钮/菜单/选项文字"
                        value={(step.structured_step?.target_name as string) || ''}
                        onChange={(v) => patchStructured(idx, step, { target_name: v })}
                      />
                    </div>
                    {showValue && (
                      <div className={styles['step-field']}>
                        <label className={styles['step-label']}>值</label>
                        <Input
                          size="small"
                          placeholder="输入内容 / 选项"
                          title="值：输入内容、下拉选项或等待文案"
                          value={step.structured_step?.value == null ? '' : String(step.structured_step.value)}
                          onChange={(v) => patchStructured(idx, step, { value: v })}
                        />
                      </div>
                    )}
                    {showDisambiguation && (
                      <div className={styles['step-field']}>
                        <label className={styles['step-label']}>消歧</label>
                        <Input
                          size="small"
                          placeholder="同名时补充"
                          title="消歧：同名控件时补充位置或上下文，如「搜索结果中的」"
                          value={(step.structured_step?.disambiguation as string) || ''}
                          onChange={(v) => patchStructured(idx, step, { disambiguation: v })}
                        />
                      </div>
                    )}
                    {showIconHint && (
                      <div className={`${styles['step-field']} ${styles['step-field-full']}`}>
                        <label className={styles['step-label']}>图标描述</label>
                        <Input
                          size="small"
                          placeholder="位置 + 外观 + 用途"
                          title="图标描述：位置、外观与用途，如「【X】形状的关闭标志」"
                          value={(step.structured_step?.icon_hint as string) || ''}
                          onChange={(v) => patchStructured(idx, step, { icon_hint: v })}
                        />
                      </div>
                    )}
                    {showNote && (
                      <div className={`${styles['step-field']} ${styles['step-field-full']}`}>
                        <label className={styles['step-label']}>备注</label>
                        <Input
                          size="small"
                          placeholder="等待/循环等补充说明"
                          title="备注：保留 Instant 里等待、关闭全部等补充意图"
                          value={(step.structured_step?.note as string) || ''}
                          onChange={(v) => patchStructured(idx, step, { note: v })}
                        />
                      </div>
                    )}
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
                            onClick={() => {
                              const next = { ...(step.structured_step || {}) };
                              delete next.selector;
                              const desc = renderStructuredDescription(next);
                              onUpdate(idx, '_patch', {
                                structured_step: next,
                                description: desc || step.description || '',
                              });
                            }}
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
