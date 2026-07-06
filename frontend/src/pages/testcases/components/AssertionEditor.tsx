import React from 'react';
import { Select, Input, InputNumber, Space, Tag } from '@arco-design/web-react';
import styles from '../style/components.module.less';

type AssertionType = 'url_contains' | 'text_exists' | 'element_visible' | 'input_value' | 'element_count';

interface AssertionConfig {
  type: AssertionType;
  selector?: string;
  text?: string;
  url_contains?: string;
  value?: string;
  count?: number;
}

const ASSERTION_LABELS: Record<AssertionType, string> = {
  url_contains: 'URL 包含',
  text_exists: '文本存在',
  element_visible: '元素可见',
  input_value: '输入值匹配',
  element_count: '元素数量',
};

function parseAssertion(raw: string | null | undefined): AssertionConfig | null {
  if (!raw) return null;
  try {
    const parsed = JSON.parse(raw);
    if (parsed && parsed.type) return parsed as AssertionConfig;
    return null;
  } catch {
    return null;
  }
}

function serializeAssertion(cfg: AssertionConfig): string {
  return JSON.stringify(cfg);
}

interface AssertionEditorProps {
  value: string | null | undefined;
  onChange: (value: string) => void;
}

const AssertionEditor: React.FC<AssertionEditorProps> = ({ value, onChange }) => {
  const cfg = parseAssertion(value);

  const setType = (type: AssertionType) => {
    const base: AssertionConfig = { type };
    if (type === 'url_contains') base.url_contains = '';
    else if (type === 'text_exists') { base.selector = ''; base.text = ''; }
    else if (type === 'element_visible') base.selector = '';
    else if (type === 'input_value') { base.selector = ''; base.value = ''; }
    else if (type === 'element_count') { base.selector = ''; base.count = 0; }
    onChange(serializeAssertion(base));
  };

  const updateField = (field: string, val: string | number) => {
    if (!cfg) return;
    onChange(serializeAssertion({ ...cfg, [field]: val }));
  };

  if (!cfg) {
    return (
      <div>
        <Select placeholder="选择断言类型" onChange={(v) => setType(v as AssertionType)} style={{ width: '100%' }}>
          {Object.entries(ASSERTION_LABELS).map(([k, v]) => (
            <Select.Option key={k} value={k}>{v}</Select.Option>
          ))}
        </Select>
        <div style={{ marginTop: 4 }}>
          <Tag color="arcoblue">原始值</Tag>
          <Input.TextArea
            value={value || ''}
            onChange={(v) => onChange(v)}
            autoSize={{ minRows: 1 }}
            placeholder="或直接输入预期结果文本"
          />
        </div>
      </div>
    );
  }

  return (
    <div>
      <Space style={{ marginBottom: 8 }}>
        <Select value={cfg.type} onChange={(v) => setType(v as AssertionType)} style={{ width: 140 }}>
          {Object.entries(ASSERTION_LABELS).map(([k, v]) => (
            <Select.Option key={k} value={k}>{v}</Select.Option>
          ))}
        </Select>
      </Space>

      {cfg.type === 'url_contains' && (
        <Input
          placeholder="输入期望的 URL 片段"
          value={cfg.url_contains || ''}
          onChange={(v) => updateField('url_contains', v)}
        />
      )}

      {cfg.type === 'text_exists' && (
        <Space style={{ width: '100%' }}>
          <Input
            placeholder="CSS 选择器"
            value={cfg.selector || ''}
            onChange={(v) => updateField('selector', v)}
            style={{ width: '45%' }}
          />
          <Input
            placeholder="期望的文本"
            value={cfg.text || ''}
            onChange={(v) => updateField('text', v)}
            style={{ width: '55%' }}
          />
        </Space>
      )}

      {cfg.type === 'element_visible' && (
        <Input
          placeholder="CSS 选择器"
          value={cfg.selector || ''}
          onChange={(v) => updateField('selector', v)}
        />
      )}

      {cfg.type === 'input_value' && (
        <Space style={{ width: '100%' }}>
          <Input
            placeholder="CSS 选择器"
            value={cfg.selector || ''}
            onChange={(v) => updateField('selector', v)}
            style={{ width: '45%' }}
          />
          <Input
            placeholder="期望的值"
            value={cfg.value || ''}
            onChange={(v) => updateField('value', v)}
            style={{ width: '55%' }}
          />
        </Space>
      )}

      {cfg.type === 'element_count' && (
        <Space style={{ width: '100%' }}>
          <Input
            placeholder="CSS 选择器"
            value={cfg.selector || ''}
            onChange={(v) => updateField('selector', v)}
            style={{ width: '55%' }}
          />
          <InputNumber
            placeholder="期望数量"
            value={cfg.count ?? 0}
            min={0}
            onChange={(v) => updateField('count', v ?? 0)}
            style={{ width: '45%' }}
          />
        </Space>
      )}
    </div>
  );
};

export { AssertionEditor, parseAssertion, serializeAssertion };
export type { AssertionType, AssertionConfig };
