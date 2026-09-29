import React, { useState } from 'react';
import { Button, Input, Select, Space } from '@arco-design/web-react';
import { IconDelete, IconFolder, IconPlus } from '@arco-design/web-react/icon';
import CodeMirror from '@uiw/react-codemirror';
import { json } from '@codemirror/lang-json';
import { BodyType, BODY_TYPES, KeyValueItem, TestFile } from '../types';
import KeyValueTable from './KeyValueTable';
import TestFilePicker from './TestFilePicker';
import styles from '../style/index.module.less';

interface BodyEditorProps {
  value: string;
  onChange: (value: string) => void;
  type: BodyType;
  onTypeChange: (type: BodyType) => void;
  /** form/form_data 模式下的键值项（由父组件维护） */
  formItems?: KeyValueItem[];
  onFormItemsChange?: (items: KeyValueItem[]) => void;
  /** 031（US1）multipart 文件字段（path 为 platform://<id> 或变量引用） */
  files?: Array<{ key: string; path: string; content_type?: string | null }>;
  onFilesChange?: (files: Array<{ key: string; path: string; content_type?: string | null }>) => void;
  /** 测试文件选择器需要的项目上下文 */
  projectId?: number | null;
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
  files = [],
  onFilesChange,
  projectId,
}) => {
  const isForm = type === 'form' || type === 'form_data';
  const [pickerIndex, setPickerIndex] = useState<number | null>(null);

  const updateFileRow = (
    index: number,
    patch: Partial<{ key: string; path: string; content_type?: string | null }>
  ) => {
    const next = files.map((f, i) => (i === index ? { ...f, ...patch } : f));
    onFilesChange?.(next);
  };

  const handlePickFile = (file: TestFile) => {
    if (pickerIndex === null) return;
    updateFileRow(pickerIndex, { path: `platform://${file.id}` });
    setPickerIndex(null);
  };

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

      {type === 'form_data' && (
        <div className={styles.multipartFiles}>
          <div className={styles.multipartFilesHeader}>
            <span>文件字段</span>
            <Button
              size="small"
              type="text"
              icon={<IconPlus />}
              onClick={() => onFilesChange?.([...files, { key: 'file', path: '' }])}
            >
              添加文件字段
            </Button>
          </div>
          {files.length === 0 && (
            <div className={styles.bodyEmpty}>
              无文件字段；需要上传文件时点「添加文件字段」并选择平台测试文件
            </div>
          )}
          {files.map((row, index) => (
            <Space key={index} style={{ marginBottom: 6 }} wrap>
              <Input
                value={row.key}
                onChange={(v) => updateFileRow(index, { key: v })}
                placeholder="字段名"
                style={{ width: 140 }}
              />
              <Input
                value={row.path}
                onChange={(v) => updateFileRow(index, { path: v })}
                placeholder="platform://<id> 或 {{变量}}"
                style={{ width: 300 }}
              />
              <Button
                size="small"
                icon={<IconFolder />}
                onClick={() => setPickerIndex(index)}
              >
                选择文件
              </Button>
              <Button
                size="small"
                type="text"
                status="danger"
                icon={<IconDelete />}
                onClick={() => onFilesChange?.(files.filter((_, i) => i !== index))}
              />
            </Space>
          ))}
          <TestFilePicker
            visible={pickerIndex !== null}
            projectId={projectId}
            onCancel={() => setPickerIndex(null)}
            onSelect={handlePickFile}
          />
        </div>
      )}
    </div>
  );
};

export default BodyEditor;