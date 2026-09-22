import React from 'react';
import { Select, Input } from '@arco-design/web-react';
import CodeMirror from '@uiw/react-codemirror';
import { json } from '@codemirror/lang-json';
import { BodyType, BODY_TYPES, KeyValueItem } from '../types';
import KeyValueTable from './KeyValueTable';
import styles from '../style/index.module.less';

interface BodyEditorProps {
  value: string;
  onChange: (value: string) => void;
  type: BodyType;
  onTypeChange: (type: BodyType) => void;
  /** form/form_data 模式下的键值项（由父组件维护） */
  formItems?: KeyValueItem[];
  onFormItemsChange?: (items: KeyValueItem[]) => void;
}

const BODY_TYPE_LABELS: Record<BodyType, string> = {
  none: '无',
  json: 'JSON',
  form: '表单 (x-www-form-urlencoded)',
  form_data: '表单数据 (multipart)',
  raw: '原始文本',
  binary: '二进制',
};

/**
 * 请求体编辑器：类型选择 + 内容编辑。
 * json 用 CodeMirror（@codemirror/lang-json），raw 用多行输入，form 用键值表。
 */
const BodyEditor: React.FC<BodyEditorProps> = ({
  value,
  onChange,
  type,
  onTypeChange,
  formItems = [],
  onFormItemsChange,
}) => {
  const isForm = type === 'form' || type === 'form_data';

  return (
    <div className={styles.bodyEditor}>
      <div className={styles.bodyTypeRow}>
        <span className={styles.bodyTypeLabel}>类型：</span>
        <Select
          value={type}
          onChange={(v) => onTypeChange(v as BodyType)}
          style={{ width: 260 }}
          options={BODY_TYPES.map((t) => ({ label: BODY_TYPE_LABELS[t], value: t }))}
        />
      </div>

      {type === 'none' && (
        <div className={styles.bodyEmpty}>该请求无请求体</div>
      )}

      {type === 'json' && (
        <CodeMirror
          value={value}
          height="240px"
          extensions={[json()]}
          onChange={(val) => onChange(val)}
          className={styles.cmEditor}
          basicSetup={{ lineNumbers: true, foldGutter: true }}
        />
      )}

      {type === 'raw' && (
        <Input.TextArea
          value={value}
          onChange={onChange}
          autoSize={{ minRows: 6, maxRows: 16 }}
          placeholder="输入原始请求体文本"
        />
      )}

      {type === 'binary' && (
        <div className={styles.bodyEmpty}>二进制请求体暂不支持在线编辑</div>
      )}

      {isForm && (
        <KeyValueTable
          items={formItems}
          onChange={onFormItemsChange || (() => undefined)}
          keyPlaceholder="字段名"
          valuePlaceholder="字段值"
        />
      )}
    </div>
  );
};

export default BodyEditor;