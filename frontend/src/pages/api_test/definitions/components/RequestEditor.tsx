import React, { useMemo, useState } from 'react';
import {
  Button,
  Input,
  InputNumber,
  Select,
  Switch,
  Tabs,
  Tooltip,
  Typography,
} from '@arco-design/web-react';
import { IconSend } from '@arco-design/web-react/icon';
import {
  ApiAuth,
  ApiStep,
  ApiPreStep,
  DATASET_MODES,
  DATASET_NONE_VALUE,
  DatasetBinding,
  Dataset,
  DatasetMode,
  HTTP_METHODS,
  KeyValueItem,
} from '../types';
import KeyValueTable from './KeyValueTable';
import BodyEditor from './BodyEditor';
import AssertionPanel from './AssertionPanel';
import ExtractPanel from './ExtractPanel';
import VariableInput from './VariableInput';
import styles from '../style/index.module.less';

const { Text } = Typography;
const TabPane = Tabs.TabPane;

interface RequestEditorProps {
  /** 当前编辑的步骤（含 request/assertions/extractors/pre/post） */
  spec: ApiStep;
  /** 当前选中接口定义的名称（未选中接口时为空） */
  definitionName?: string | null;
  onChange: (spec: ApiStep) => void;
  /** 嵌在场景步骤里时隐藏发送、校验和数据集 */
  embedded?: boolean;
  onSend?: () => void;
  sending?: boolean;
  dryRun?: boolean;
  onDryRunChange?: (dryRun: boolean) => void;
  datasets?: Dataset[];
  datasetBinding?: DatasetBinding;
  onDatasetBindingChange?: (binding: DatasetBinding) => void;
}

/** form 内容 <-> 键值项 序列化 */
const parseFormContent = (content: string): KeyValueItem[] => {
  if (!content) return [];
  try {
    const parsed = JSON.parse(content);
    if (Array.isArray(parsed)) {
      return parsed.map((p) => ({
        key: String(p?.key ?? ''),
        value: String(p?.value ?? ''),
        enable: p?.enable !== false,
        secret: !!p?.secret,
      }));
    }
  } catch {
    /* 非 JSON，按 urlencoded 解析 */
  }
  return content
    .split('&')
    .filter(Boolean)
    .map((pair) => {
      const [k, v] = pair.split('=');
      return { key: decodeURIComponent(k || ''), value: decodeURIComponent(v || ''), enable: true };
    });
};

const serializeFormContent = (items: KeyValueItem[]): string =>
  items
    .filter((i) => i.enable && i.key)
    .map((i) => `${encodeURIComponent(i.key)}=${encodeURIComponent(i.value)}`)
    .join('&');

const AUTH_TYPES = ['none', 'basic', 'bearer', 'api_key'] as const;

/**
 * 请求编辑器（Postman 风格）：
 * 顶部 method + URL + 发送按钮；下方 7 个 tab：
 * Params / Headers / Body / 断言 / 提取 / 前置后置 / 设置。
 */
const RequestEditor: React.FC<RequestEditorProps> = ({
  spec,
  definitionName,
  onChange,
  embedded = false,
  onSend = () => undefined,
  sending = false,
  dryRun = false,
  onDryRunChange = () => undefined,
  datasets = [],
  datasetBinding = { dataset_id: null, dataset_mode: 'sequential' },
  onDatasetBindingChange = () => undefined,
}) => {
  const { request } = spec;

  const updateRequest = (patch: Partial<ApiStep['request']>) => {
    onChange({ ...spec, request: { ...request, ...patch } });
  };

  const updateStep = (patch: Partial<ApiStep>) => {
    onChange({ ...spec, ...patch });
  };

  /** form 键值项（由 body.content 反序列化而来） */
  const formItems = useMemo(
    () => (request.body.type === 'form' || request.body.type === 'form_data'
      ? parseFormContent(request.body.content)
      : []),
    [request.body.type, request.body.content]
  );

  const handleFormItemsChange = (items: KeyValueItem[]) => {
    updateRequest({ body: { ...request.body, content: serializeFormContent(items) } });
  };

  const updatePre = (index: number, patch: Partial<ApiPreStep>) => {
    const pre = spec.pre.map((p, i) => (i === index ? { ...p, ...patch } : p));
    updateStep({ pre });
  };

  const removePre = (index: number) => {
    updateStep({ pre: spec.pre.filter((_, i) => i !== index) });
  };

  const addPre = () => {
    updateStep({ pre: [...spec.pre, { type: 'set_variable', key: '', value: '' }] });
  };

  const updatePost = (index: number, patch: Partial<ApiPreStep>) => {
    const post = (spec.post || []).map((p, i) => (i === index ? { ...p, ...patch } : p));
    updateStep({ post });
  };

  const removePost = (index: number) => {
    updateStep({ post: (spec.post || []).filter((_, i) => i !== index) });
  };

  const addPost = () => {
    updateStep({ post: [...(spec.post || []), { type: 'set_variable', key: '', value: '' }] });
  };

  const auth = request.auth as ApiAuth;
  const authType = auth.type as string;

  return (
    <div className={styles.requestEditor}>
      {definitionName && (
        <div className={styles.requestDefName}>接口：{definitionName}</div>
      )}
      {/* 顶部：method + URL + 发送 */}
      <div className={styles.requestTopBar}>
        <Select
          className={styles.methodSelect}
          value={request.method}
          onChange={(v) => updateRequest({ method: v })}
          options={HTTP_METHODS.map((m) => ({ label: m, value: m }))}
        />
        <VariableInput
          value={request.url}
          onChange={(value) => updateRequest({ url: value })}
          placeholder="请求 URL（支持 {{var}}，如 {{baseUrl}}/api/users）"
          showPreview={false}
        />
        {embedded ? null : (
          <>
            <Tooltip content="仅渲染请求并校验变量，不真正发送请求">
              <Switch
                checked={dryRun}
                onChange={onDryRunChange}
                checkedText="仅校验变量"
                uncheckedText="仅校验变量"
                className={styles.dryRunSwitch}
              />
            </Tooltip>
            <Button
              type="primary"
              icon={<IconSend />}
              loading={sending}
              onClick={onSend}
              className={styles.sendBtn}
            >
              {dryRun ? '校验' : '发送'}
            </Button>
          </>
        )}
      </div>

      <Tabs defaultActiveTab="params" className={styles.requestTabs}>
        <TabPane key="params" title="Params">
          <KeyValueTable
            items={request.query}
            onChange={(items) => updateRequest({ query: items })}
            keyPlaceholder="参数名"
            valuePlaceholder="参数值"
          />
        </TabPane>

        <TabPane key="headers" title="Headers">
          <KeyValueTable
            items={request.headers}
            onChange={(items) => updateRequest({ headers: items })}
            keyPlaceholder="Header 名"
            valuePlaceholder="Header 值"
            withSecret
          />
        </TabPane>

        <TabPane key="body" title="Body">
          <BodyEditor
            value={request.body.content}
            onChange={(content) => updateRequest({ body: { ...request.body, content } })}
            type={request.body.type}
            onTypeChange={(type) => updateRequest({ body: { ...request.body, type } })}
            formItems={formItems}
            onFormItemsChange={handleFormItemsChange}
          />
        </TabPane>

        <TabPane key="assertions" title="断言">
          <AssertionPanel
            items={spec.assertions}
            onChange={(items) => updateStep({ assertions: items })}
          />
        </TabPane>

        <TabPane key="extractors" title="提取">
          <ExtractPanel
            items={spec.extractors}
            onChange={(items) => updateStep({ extractors: items })}
          />
        </TabPane>

        <TabPane key="prepost" title="前置后置">
          <div className={styles.prePostSection}>
            <div className={styles.prePostTitle}>前置操作（请求发送前执行）</div>
            {spec.pre.length === 0 && (
              <div className={styles.panelEmpty}>暂无前置操作</div>
            )}
            {spec.pre.map((p, index) => (
              <div className={styles.preRow} key={index}>
                <Select
                  className={styles.preType}
                  value={p.type}
                  onChange={(v) => updatePre(index, { type: v as ApiPreStep['type'] })}
                  options={[
                    { label: '设置变量', value: 'set_variable' },
                    { label: '延时', value: 'delay' },
                  ]}
                />
                {p.type === 'set_variable' ? (
                  <>
                    <Input
                      className={styles.preField}
                      placeholder="变量名"
                      value={p.key || ''}
                      onChange={(value) => updatePre(index, { key: value })}
                    />
                    <Input
                      className={styles.preField}
                      placeholder="变量值（支持 {{var}}）"
                      value={p.value || ''}
                      onChange={(value) => updatePre(index, { value: value })}
                    />
                  </>
                ) : (
                  <InputNumber
                    className={styles.preField}
                    placeholder="延时毫秒"
                    value={p.ms ?? 0}
                    min={0}
                    onChange={(value) => updatePre(index, { ms: value ?? 0 })}
                  />
                )}
                <Button
                  size="mini"
                  type="text"
                  status="danger"
                  onClick={() => removePre(index)}
                >
                  删除
                </Button>
              </div>
            ))}
            <Button size="small" type="outline" onClick={addPre} className={styles.panelAddBtn}>
              + 添加前置操作
            </Button>
            <div className={styles.prePostTitle} style={{ marginTop: 16 }}>
              后置操作（响应返回后执行，变量可供后续步骤使用）
            </div>
            {(spec.post || []).length === 0 && (
              <div className={styles.panelEmpty}>暂无后置操作</div>
            )}
            {(spec.post || []).map((p, index) => (
              <div className={styles.preRow} key={`post-${index}`}>
                <Select
                  className={styles.preType}
                  value={p.type}
                  onChange={(v) => updatePost(index, { type: v as ApiPreStep['type'] })}
                  options={[
                    { label: '设置变量', value: 'set_variable' },
                    { label: '延时', value: 'delay' },
                  ]}
                />
                {p.type === 'set_variable' ? (
                  <>
                    <Input
                      className={styles.preField}
                      placeholder="变量名"
                      value={p.key || ''}
                      onChange={(value) => updatePost(index, { key: value })}
                    />
                    <Input
                      className={styles.preField}
                      placeholder="变量值（支持 {{var}}）"
                      value={p.value || ''}
                      onChange={(value) => updatePost(index, { value: value })}
                    />
                  </>
                ) : (
                  <InputNumber
                    className={styles.preField}
                    placeholder="延时毫秒"
                    value={p.ms ?? 0}
                    min={0}
                    onChange={(value) => updatePost(index, { ms: value ?? 0 })}
                  />
                )}
                <Button size="mini" type="text" status="danger" onClick={() => removePost(index)}>
                  删除
                </Button>
              </div>
            ))}
            <Button size="small" type="outline" onClick={addPost} className={styles.panelAddBtn}>
              + 添加后置操作
            </Button>
          </div>
        </TabPane>

        <TabPane key="settings" title="设置">
          <div className={styles.settingsGrid}>
            {embedded ? null : (
            <>
            <div className={styles.settingsRow}>
              <span className={styles.settingsLabel}>数据集</span>
              <Select
                value={datasetBinding.dataset_id ?? DATASET_NONE_VALUE}
                onChange={(value) =>
                  onDatasetBindingChange({
                    ...datasetBinding,
                    dataset_id: value === DATASET_NONE_VALUE ? null : Number(value),
                  })
                }
                style={{ width: 200 }}
                options={[
                  { label: '不使用', value: DATASET_NONE_VALUE },
                  ...datasets.map((d) => ({
                    label: `${d.name}（${Array.isArray(d.rows) ? d.rows.length : 0} 行）`,
                    value: d.id,
                  })),
                ]}
              />
            </div>
            <div className={styles.settingsRow}>
              <span className={styles.settingsLabel}>迭代模式</span>
              <Select
                value={datasetBinding.dataset_mode}
                onChange={(value) =>
                  onDatasetBindingChange({ ...datasetBinding, dataset_mode: value as DatasetMode })
                }
                disabled={datasetBinding.dataset_id == null}
                style={{ width: 200 }}
                options={DATASET_MODES}
              />
            </div>
            </>
            )}
            <div className={styles.settingsRow}>
              <span className={styles.settingsLabel}>超时（ms）</span>
              <InputNumber
                value={request.timeout_ms}
                min={1000}
                max={300000}
                step={1000}
                onChange={(value) => updateRequest({ timeout_ms: value ?? 30000 })}
                style={{ width: 200 }}
              />
            </div>
            <div className={styles.settingsRow}>
              <span className={styles.settingsLabel}>跟随重定向</span>
              <Switch
                checked={request.follow_redirects}
                onChange={(checked) => updateRequest({ follow_redirects: checked })}
              />
            </div>
            <div className={styles.settingsRow}>
              <span className={styles.settingsLabel}>校验 SSL 证书</span>
              <Switch
                checked={request.verify_ssl}
                onChange={(checked) => updateRequest({ verify_ssl: checked })}
              />
            </div>
            <div className={styles.settingsRow}>
              <span className={styles.settingsLabel}>认证方式</span>
              <Select
                value={authType}
                onChange={(v) => updateRequest({ auth: { type: v as ApiAuth['type'] } })}
                style={{ width: 200 }}
                options={AUTH_TYPES.map((t) => ({ label: t, value: t }))}
              />
            </div>
            {auth.type === 'basic' && (
              <>
                <div className={styles.settingsRow}>
                  <span className={styles.settingsLabel}>用户名</span>
                  <Input
                    value={String(auth.username ?? '')}
                    onChange={(value) => updateRequest({ auth: { ...auth, username: value } })}
                    style={{ width: 200 }}
                  />
                </div>
                <div className={styles.settingsRow}>
                  <span className={styles.settingsLabel}>密码</span>
                  <Input.Password
                    value={String(auth.password ?? '')}
                    onChange={(value) => updateRequest({ auth: { ...auth, password: value } })}
                    style={{ width: 200 }}
                  />
                </div>
              </>
            )}
            {auth.type === 'bearer' && (
              <div className={styles.settingsRow}>
                <span className={styles.settingsLabel}>Token</span>
                <Input
                  value={String(auth.token ?? '')}
                  onChange={(value) => updateRequest({ auth: { ...auth, token: value } })}
                  style={{ width: 200 }}
                  placeholder="支持 {{var}}"
                />
              </div>
            )}
            {auth.type === 'api_key' && (
              <>
                <div className={styles.settingsRow}>
                  <span className={styles.settingsLabel}>Key 名</span>
                  <Input
                    value={String(auth.key ?? '')}
                    onChange={(value) => updateRequest({ auth: { ...auth, key: value } })}
                    style={{ width: 200 }}
                  />
                </div>
                <div className={styles.settingsRow}>
                  <span className={styles.settingsLabel}>Key 值</span>
                  <Input
                    value={String(auth.value ?? '')}
                    onChange={(value) => updateRequest({ auth: { ...auth, value: value } })}
                    style={{ width: 200 }}
                  />
                </div>
                <div className={styles.settingsRow}>
                  <span className={styles.settingsLabel}>位置</span>
                  <Select
                    value={auth.in as string}
                    onChange={(v) => updateRequest({ auth: { ...auth, in: v } })}
                    style={{ width: 200 }}
                    options={[
                      { label: 'Header', value: 'header' },
                      { label: 'Query', value: 'query' },
                    ]}
                  />
                </div>
              </>
            )}
          </div>
        </TabPane>
      </Tabs>
    </div>
  );
};

export default RequestEditor;