import React from 'react';
import { Button, Input, Select, Switch } from '@arco-design/web-react';
import { IconDelete, IconPlus } from '@arco-design/web-react/icon';
import { ApiExtractor, EXTRACTOR_TYPES, createExtractor } from '../types';
import styles from '../style/index.module.less';

interface ExtractPanelProps {
  items: ApiExtractor[];
  onChange: (items: ApiExtractor[]) => void;
}

const TYPE_LABELS: Record<string, string> = {
  jsonpath: 'JSONPath',
  regex: '正则',
  header: '响应头',
  cookie: 'Cookie',
};

/**
 * 提取器面板：增删改（type/expression/variable/scope/required/enable）。
 * 对齐 data-model §3 extractors[] 契约。
 */
const ExtractPanel: React.FC<ExtractPanelProps> = ({ items, onChange }) => {
  const updateItem = (index: number, patch: Partial<ApiExtractor>) => {
    onChange(items.map((item, i) => (i === index ? { ...item, ...patch } : item)));
  };

  const removeItem = (index: number) => {
    onChange(items.filter((_, i) => i !== index));
  };

  const addItem = () => {
    onChange([...items, createExtractor()]);
  };

  return (
    <div className={styles.panelList}>
      {items.length === 0 && (
        <div className={styles.panelEmpty}>暂无提取器，点击「添加提取器」新增</div>
      )}
      {items.map((item, index) => (
        <div className={styles.panelItem} key={index}>
          <div className={styles.panelItemHeader}>
            <Switch
              size="small"
              checked={item.enable}
              onChange={(checked) => updateItem(index, { enable: checked })}
              aria-label="启用提取器"
            />
            <Select
              className={styles.panelItemField}
              value={item.type}
              onChange={(v) => updateItem(index, { type: v })}
              options={EXTRACTOR_TYPES.map((t) => ({ label: TYPE_LABELS[t] || t, value: t }))}
            />
            <Input
              className={styles.panelItemField}
              placeholder="表达式（如 $.data.token）"
              value={item.expression}
              onChange={(value) => updateItem(index, { expression: value })}
            />
            <Input
              className={styles.panelItemField}
              placeholder="变量名（如 token）"
              value={item.variable}
              onChange={(value) => updateItem(index, { variable: value })}
            />
            <Select
              className={styles.panelItemField}
              value={item.scope}
              onChange={(v) => updateItem(index, { scope: v as 'case' | 'environment' })}
              options={[
                { label: '用例级 (case)', value: 'case' },
                { label: '环境级 (environment)', value: 'environment' },
              ]}
            />
            <span className={styles.panelItemRequired}>
              <Switch
                size="small"
                checked={item.required}
                onChange={(checked) => updateItem(index, { required: checked })}
                aria-label="提取失败即失败"
              />
              <span className={styles.panelItemRequiredLabel}>必取</span>
            </span>
            <Button
              size="mini"
              type="text"
              status="danger"
              icon={<IconDelete />}
              onClick={() => removeItem(index)}
              aria-label="删除提取器"
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
        添加提取器
      </Button>
    </div>
  );
};

export default ExtractPanel;