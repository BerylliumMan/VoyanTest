import React, { useState } from 'react';
import axios from 'axios';
import { Button, Checkbox, Dropdown, Input, Menu, Message, Modal, Select, Switch, Tooltip } from '@arco-design/web-react';
import { IconDelete, IconDown, IconPlus } from '@arco-design/web-react/icon';
import CodeMirror from '@uiw/react-codemirror';
import {
  ApiAssertion,
  ASSERTION_TYPES,
  ASSERTION_CONDITIONS,
  ASSERTION_TEMPLATES,
  createAssertion,
} from '../types';
import styles from '../style/index.module.less';

interface AssertionPanelProps {
  items: ApiAssertion[];
  onChange: (items: ApiAssertion[]) => void;
  /** 031（US11）：最近一次响应样本（AI 断言建议的输入；无样本时入口禁用） */
  responseSample?: {
    method?: string;
    url?: string;
    status_code?: number;
    headers?: Record<string, string>;
    body_text?: string;
  } | null;
}

interface SuggestedAssertion {
  type: string;
  condition: string;
  expression?: string;
  expected?: unknown;
  name?: string;
}

const TYPE_LABELS: Record<string, string> = {
  expression: '表达式',
  status_code: '状态码',
  jsonpath: 'JSONPath',
  header: '响应头',
  body_contains: '响应体包含',
  body_regex: '响应体正则',
  response_time: '响应时间',
  jsonschema: 'JSON Schema',
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

/** 031（US6）：表达式断言单独渲染（CodeMirror + 无 condition/expected） */
const isExpressionType = (t: string) => t === 'expression';

const EXPRESSION_HINT =
  "可用：json（响应体）· status_code · duration_ms · headers · vars（变量）；结果必须是布尔值。示例：len(json['data']['items']) == json['data']['total']";

/**
 * 断言面板：增删改（type/condition/expected/name/enable）。
 * 对齐 data-model §3 assertions[] 契约；031 增表达式断言与模板库（FR-011/FR-012）。
 */
const AssertionPanel: React.FC<AssertionPanelProps> = ({ items, onChange, responseSample }) => {
  // 031（US11）：AI 断言建议（服务端已做 schema 校验）；503 → 隐藏入口降级
  const [aiVisible, setAiVisible] = useState(false);
  const [aiLoading, setAiLoading] = useState(false);
  const [aiCandidates, setAiCandidates] = useState<SuggestedAssertion[]>([]);
  const [aiChecked, setAiChecked] = useState<boolean[]>([]);
  const [aiUnavailable, setAiUnavailable] = useState(false);

  const requestSuggestions = async () => {
    if (!responseSample) return;
    setAiLoading(true);
    try {
      const data = await axios.post('/api/api-test/debug/suggest-assertions', {
        method: responseSample.method || 'GET',
        url: responseSample.url || '',
        status_code: responseSample.status_code ?? null,
        headers: responseSample.headers || {},
        body_text: responseSample.body_text || '',
      });
      const list: SuggestedAssertion[] = data.data?.suggestions || [];
      setAiCandidates(list);
      setAiChecked(list.map(() => true));
      setAiVisible(true);
    } catch (e: unknown) {
      const status = (e as { response?: { status?: number } })?.response?.status;
      if (status === 503) {
        setAiUnavailable(true);
        Message.warning('AI 服务当前不可用，已隐藏 AI 断言建议');
      } else {
        Message.error('AI 断言建议失败');
      }
    } finally {
      setAiLoading(false);
    }
  };

  const applySuggestions = () => {
    const picked = aiCandidates.filter((_, i) => aiChecked[i]);
    if (picked.length === 0) {
      Message.warning('请至少选择一条候选');
      return;
    }
    onChange([
      ...items,
      ...picked.map((c) => ({
        ...createAssertion(),
        type: c.type as ApiAssertion['type'],
        condition: c.condition as ApiAssertion['condition'],
        expression: c.expression || '',
        expected: (c.expected ?? '') as ApiAssertion['expected'],
        name: c.name || '',
      })),
    ]);
    setAiVisible(false);
    Message.success(`已加入 ${picked.length} 条断言`);
  };

  const updateItem = (index: number, patch: Partial<ApiAssertion>) => {
    onChange(items.map((item, i) => (i === index ? { ...item, ...patch } : item)));
  };

  const removeItem = (index: number) => {
    onChange(items.filter((_, i) => i !== index));
  };

  const addItem = () => {
    onChange([...items, createAssertion()]);
  };

  const addFromTemplate = (key: string) => {
    const tpl = ASSERTION_TEMPLATES.find((t) => t.key === key);
    if (!tpl) return;
    onChange([...items, tpl.build()]);
  };

  const aiModal = (
    <Modal
      title="AI 断言候选（已通过服务端校验）"
      visible={aiVisible}
      onCancel={() => setAiVisible(false)}
      onOk={applySuggestions}
      okText="加入断言"
    >
      {aiCandidates.map((c, i) => (
        <div key={i} style={{ marginBottom: 8 }}>
          <Checkbox
            checked={aiChecked[i] || false}
            onChange={(v) => setAiChecked((prev) => prev.map((x, j) => (j === i ? v : x)))}
          >
            <b>{c.name || c.type}</b>
            <span style={{ marginLeft: 8, color: 'var(--vt-text-3)', fontSize: 12 }}>
              {c.type} / {c.condition}
              {c.expression ? ` · ${c.expression}` : ''}
              {c.expected !== undefined && c.expected !== null && c.expected !== ''
                ? ` · 期望 ${String(c.expected)}`
                : ''}
            </span>
          </Checkbox>
        </div>
      ))}
    </Modal>
  );

  return (
    <div className={styles.panelList}>
      {aiModal}
      {items.length === 0 && (
        <div className={styles.panelEmpty}>暂无断言，点击「添加断言」或从模板添加</div>
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
              onChange={(v) =>
                updateItem(
                  index,
                  v === 'expression'
                    ? { type: v, condition: 'expression' }
                    : { type: v, condition: item.condition === 'expression' ? 'equals' : item.condition }
                )
              }
              options={ASSERTION_TYPES.map((t) => ({ label: TYPE_LABELS[t] || t, value: t }))}
            />
            {isExpressionType(item.type) ? (
              <div className={styles.panelExpression} style={{ flex: 1, minWidth: 320 }}>
                <Tooltip content={EXPRESSION_HINT}>
                  <div className={styles.panelExpressionEditor}>
                    <CodeMirror
                      value={item.expression || ''}
                      height="48px"
                      basicSetup={{ lineNumbers: false, foldGutter: false, highlightActiveLine: false }}
                      onChange={(value) => updateItem(index, { expression: value })}
                    />
                  </div>
                </Tooltip>
              </div>
            ) : (
              <>
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
              </>
            )}
          </div>
          {isExpressionType(item.type) && (
            <div className={styles.panelItemHint} style={{ fontSize: 12, color: 'var(--vt-text-3)', paddingLeft: 4 }}>
              {EXPRESSION_HINT}
            </div>
          )}
        </div>
      ))}
      <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
        <Button
          size="small"
          type="outline"
          icon={<IconPlus />}
          onClick={addItem}
          className={styles.panelAddBtn}
        >
          添加断言
        </Button>
        <Dropdown
          trigger="click"
          droplist={
            <Menu onClickMenuItem={(key) => addFromTemplate(String(key))}>
              {ASSERTION_TEMPLATES.map((t) => (
                <Menu.Item key={t.key}>
                  <div>
                    <div>{t.label}</div>
                    <div style={{ fontSize: 11, color: 'var(--vt-text-3)' }}>{t.hint}</div>
                  </div>
                </Menu.Item>
              ))}
            </Menu>
          }
        >
          <Button size="small" type="text" icon={<IconDown />}>
            从模板添加
          </Button>
        </Dropdown>
        {!aiUnavailable && (
          <Button
            size="small"
            type="outline"
            loading={aiLoading}
            disabled={!responseSample}
            onClick={requestSuggestions}
            title={responseSample ? '根据最近一次响应生成候选断言' : '先发送一次请求再生成'}
          >
            AI 生成断言
          </Button>
        )}
      </div>
    </div>
  );
};

export default AssertionPanel;
