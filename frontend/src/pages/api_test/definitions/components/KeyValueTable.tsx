import React from 'react';
import { Button, Input, Switch, Tooltip } from '@arco-design/web-react';
import { IconDelete, IconPlus, IconLock } from '@arco-design/web-react/icon';
import { KeyValueItem, createKeyValue } from '../types';
import styles from '../style/index.module.less';

interface KeyValueTableProps {
  items: KeyValueItem[];
  onChange: (items: KeyValueItem[]) => void;
  keyPlaceholder?: string;
  valuePlaceholder?: string;
  /** 是否显示 secret 开关（如变量/请求头） */
  withSecret?: boolean;
}

/**
 * 通用键值表：key / value / enable 开关 / 删除 / 新增，可选 secret 标记。
 * 用于 Params、Headers、form 表单等场景。
 */
const KeyValueTable: React.FC<KeyValueTableProps> = ({
  items,
  onChange,
  keyPlaceholder = 'Key',
  valuePlaceholder = 'Value',
  withSecret = false,
}) => {
  const updateItem = (index: number, patch: Partial<KeyValueItem>) => {
    const next = items.map((item, i) => (i === index ? { ...item, ...patch } : item));
    onChange(next);
  };

  const removeItem = (index: number) => {
    onChange(items.filter((_, i) => i !== index));
  };

  const addItem = () => {
    onChange([...items, createKeyValue()]);
  };

  return (
    <div className={styles.kvTable}>
      {items.length === 0 && (
        <div className={styles.kvEmpty}>暂无参数，点击下方「添加」按钮新增</div>
      )}
      {items.map((item, index) => (
        <div className={styles.kvRow} key={index}>
          <Switch
            size="small"
            checked={item.enable}
            onChange={(checked) => updateItem(index, { enable: checked })}
            className={styles.kvSwitch}
            aria-label="启用"
          />
          <Input
            className={styles.kvInput}
            placeholder={keyPlaceholder}
            value={item.key}
            onChange={(value) => updateItem(index, { key: value })}
          />
          <Input
            className={styles.kvInput}
            placeholder={valuePlaceholder}
            value={item.value}
            onChange={(value) => updateItem(index, { value: value })}
          />
          {withSecret && (
            <Tooltip content={item.secret ? '敏感值（显示打码）' : '标记为敏感值'}>
              <Button
                size="mini"
                type={item.secret ? 'primary' : 'secondary'}
                icon={<IconLock />}
                className={styles.kvSecretBtn}
                onClick={() => updateItem(index, { secret: !item.secret })}
                aria-label="标记敏感值"
              />
            </Tooltip>
          )}
          <Button
            size="mini"
            type="text"
            status="danger"
            icon={<IconDelete />}
            onClick={() => removeItem(index)}
            aria-label="删除"
          />
        </div>
      ))}
      <Button
        size="small"
        type="outline"
        icon={<IconPlus />}
        onClick={addItem}
        className={styles.kvAddBtn}
      >
        添加
      </Button>
    </div>
  );
};

export default KeyValueTable;