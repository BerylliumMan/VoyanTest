import React from 'react';
import { Button, Input, Select, Switch, Tooltip } from '@arco-design/web-react';
import { IconDelete, IconPlus } from '@arco-design/web-react/icon';
import {
  ApiAssertion,
  ASSERTION_TYPES,
  ASSERTION_CONDITIONS,
  createAssertion,
} from '../types';
import styles from '../style/index.module.less';

interface AssertionPanelProps {
  items: ApiAssertion[];
  onChange: (items: ApiAssertion[]) => void;
}

const TYPE_LABELS: Record<string, string> = {
  status_code: '状态码',
  jsonpath: 'JSONPath',
  header: '响应头',
  body_contains: '响应体包含',
  body_regex: '响应体正则',
  response_time: '响应时间',
};

const CONDITION_LABELS: Record<string, string> = {
  equals: '等于',
  not_equals: '不等于',
  contains: '包含',
  not_contains: '不包含',
  gt: '大于',
  gte: '大于等于',
  lt: '小于',
  lte: '小于等于',
  exists: '存在',
  not_exists: '不存在',
  regex: '正则匹配',
};

/** 需要表达式输入框的断言类型 */
const NEEDS_EXPRESSION = new Set(['jsonpath', 'header', 'body_regex']);

/**
 * 断言面板：增删改（type/condition/expected/name/enable）。
 * 对齐 data-model §3 assertions[] 契约。
 */
const AssertionPanel: React.FC<AssertionPanelProps> = ({ items, onChange }) => {
  const updateItem = (index: number, patch: Partial<ApiAssertion>) => {
    onChange(items.map((item, i) => (i === index ? { ...item, ...patch } : item)));
  };

  const removeItem = (index: number) => {
    onChange(items.filter((_, i) => i !== index));
  };

  const addItem = () => {
    onChange([...items, createAssertion()]);
  };

  return (
    <div className={styles.panelList}>
      {items.length === 0 && (
        <div className={styles.panelEmpty}>暂无断言，点击「添加断言」新增</div>
      )}
      {items.map((item, index) => (
        <div className={styles.panelItem} key={index}>
          <div className={styles.panelItemHeader}>
            <Switch
              size="small"
              checked={item.enable}
              onChange={(checked) => updateItem(index, { enable: checked })}
              aria-label="启用断言"
            />
            <Input
              className={styles.panelItemName}
              placeholder="断言名称（可选）"
              value={item.name}
              onChange={(value) => updateItem(index, { name: value })}
            />
            <Button
              size="mini"
              type="text"
              status="danger"
              icon={<IconDelete />}
              onClick={() => removeItem(index)}
              aria-label="删除断言"
            />
          </div>
          <div className={styles.panelItemBody}>
            <Select
              className={styles.panelItemField}
              value={item.type}
              onChange={(v) => updateItem(index, { type: v })}
              options={ASSERTION_TYPES.map((t) => ({ label: TYPE_LABELS[t] || t, value: t }))}
            />
            {NEEDS_EXPRESSION.has(item.type) && (
              <Input
                className={styles.panelItemField}
                placeholder="表达式（如 $.data.token）"
                value={item.expression || ''}
                onChange={(value) => updateItem(index, { expression: value })}
              />
            )}
            <Select
              className={styles.panelItemField}
              value={item.condition}
              onChange={(v) => updateItem(index, { condition: v })}
              options={ASSERTION_CONDITIONS.map((c) => ({
                label: CONDITION_LABELS[c] || c,
                value: c,
              }))}
            />
            <Input
              className={styles.panelItemField}
              placeholder="期望值"
              value={item.expected}
              onChange={(value) => updateItem(index, { expected: value })}
            />
          </div>
        </div>
      ))}
      <Button
        size="small"
        type="outline"
        icon={<IconPlus />}
        onClick={addItem}
        className={styles.panelAddBtn}
      >
        添加断言
      </Button>
    </div>
  );
};

export default AssertionPanel;