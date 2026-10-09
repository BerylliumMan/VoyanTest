import React, { useEffect, useState } from 'react';
import { Form, Input, Modal, Select, Table, Tag, Typography } from '@arco-design/web-react';
import { apiPut } from '@/utils/apiRequest';
import { EnvVariablesList, EnvHeadersList, EnvServicesList } from '@/components/EnvVariablesEditor';
import {
  serializeEnvHeaders,
  serializeEnvVariables,
  toEnvHeadersForm,
  toEnvVariablesForm,
  toEnvServicesForm,
  serializeEnvServices,
} from '@/pages/testcases/utils/envPayload';
import { AuthConfig, AuthOAuth2Config, EnvVariableItem, Environment } from '../types';
import styles from '../style/index.module.less';

const { Text } = Typography;

/** 033（US3）：environment.default_auth → 表单摊平（secret 字段已打码，保持 ****** 即不修改）。 */
export const toAuthForm = (auth?: AuthConfig | null) => {
  const oauth2 = auth?.oauth2 || {};
  return {
    auth_type: auth?.type || 'none',
    auth_username: auth?.username || '',
    auth_password: auth?.password || '',
    auth_token: auth?.token || '',
    auth_key_name: auth?.key_name || '',
    auth_key_value: auth?.key_value || '',
    auth_in: auth?.in_ || 'header',
    auth_grant: oauth2.grant || 'client_credentials',
    auth_token_url: oauth2.token_url || '',
    auth_client_id: oauth2.client_id || '',
    auth_client_secret: oauth2.client_secret || '',
    auth_scope: oauth2.scope || '',
    auth_oauth_username: oauth2.username || '',
    auth_oauth_password: oauth2.password || '',
    auth_refresh_token: oauth2.refresh_token || '',
    auth_client_auth: oauth2.client_auth || 'body',
    auth_token_var: oauth2.token_var || '',
  };
};

const compactAuthValues = (obj: Record<string, unknown>): Record<string, unknown> => {
  const out: Record<string, unknown> = {};
  Object.entries(obj).forEach(([k, v]) => {
    if (v !== undefined && v !== null && v !== '') out[k] = v;
  });
  return out;
};

/** 表单值 → PUT 负载（none → null 清除；****** 原样回传，后端保留原值）。 */
export const serializeDefaultAuth = (
  values: Record<string, unknown> | undefined
): AuthConfig | null => {
  const type = String(values?.auth_type || 'none') as AuthConfig['type'];
  if (type === 'none') return null;
  if (type === 'basic') {
    return compactAuthValues({
      type,
      username: values?.auth_username,
      password: values?.auth_password,
    }) as unknown as AuthConfig;
  }
  if (type === 'bearer') {
    return compactAuthValues({ type, token: values?.auth_token }) as unknown as AuthConfig;
  }
  if (type === 'api_key') {
    return compactAuthValues({
      type,
      key_name: values?.auth_key_name,
      key_value: values?.auth_key_value,
      in_: values?.auth_in,
    }) as unknown as AuthConfig;
  }
  return {
    type: 'oauth2',
    oauth2: compactAuthValues({
      grant: values?.auth_grant || 'client_credentials',
      token_url: values?.auth_token_url,
      client_id: values?.auth_client_id,
      client_secret: values?.auth_client_secret,
      scope: values?.auth_scope,
      username: values?.auth_oauth_username,
      password: values?.auth_oauth_password,
      refresh_token: values?.auth_refresh_token,
      client_auth: values?.auth_client_auth || 'body',
      token_var: values?.auth_token_var,
    }) as AuthOAuth2Config,
  };
};

interface EnvVariablesModalProps {
  visible: boolean;
  onClose: () => void;
  /** 当前选中的环境（含后端已打码的 variables/headers） */
  environment: Environment | null;
  /** 保存成功后通知父级刷新环境列表 */
  onSaved: () => void;
}

interface EnvVariableFormShape {
  variables?: Parameters<typeof serializeEnvVariables>[0];
  headers?: Parameters<typeof serializeEnvHeaders>[0];
  /** 031（US4）多域名服务（表单列表 ↔ 后端 dict） */
  services_list?: Parameters<typeof serializeEnvServices>[0];
  /** 033（US3）默认认证（表单摊平值） */
  auth?: Record<string, unknown>;
}

const T: Record<string, string> = {
  'environment.service.name': '服务名',
  'environment.service.name_required': '服务名必填',
  'environment.service.url': '服务地址（绝对 URL）',
  'environment.service.url_invalid': '必须是绝对 URL（http(s)://…）或留空',
  'environment.service.add': '添加服务',
  'environment.service.hint': '模板中用 {{服务名}} 引用（优先级高于普通环境变量）',
  'environment.variable.key': '变量名',
  'environment.variable.key_required': '请输入变量名',
  'environment.variable.key_placeholder': '如 token',
  'environment.variable.value': '变量值',
  'environment.variable.value_placeholder': '变量值',
  'environment.variable.not_set': '未设置',
  'environment.variable.secret': '加密',
  'environment.variable.enable': '启用',
  'environment.variable.remove': '删除变量',
  'environment.variable.secret_hint':
    '加密变量以 ****** 回显；保持 ****** 表示不修改原值，输入新值即覆盖。',
  'environment.variable.add': '添加变量',
  'environment.header.key': 'Header 名',
  'environment.header.key_required': '请输入 Header 名',
  'environment.header.key_placeholder': '如 X-Token',
  'environment.header.value': 'Header 值',
  'environment.header.value_placeholder': 'Header 值',
  'environment.header.enable': '启用',
  'environment.header.remove': '删除请求头',
  'environment.header.add': '添加公共请求头',
  actions: '操作',
};

const displayValue = (item: EnvVariableItem): string => {
  if (!item.secret) return item.value || '';
  return item.has_value === false ? '未设置' : '******';
};

const EnvVariablesModal: React.FC<EnvVariablesModalProps> = ({
  visible,
  onClose,
  environment,
  onSaved,
}) => {
  const [form] = Form.useForm();
  const [saving, setSaving] = useState(false);
  // 033（US3）：默认认证（条件渲染需跟随表单当前值）
  const [authType, setAuthType] = useState<AuthConfig['type']>('none');
  const [authGrant, setAuthGrant] = useState<NonNullable<AuthOAuth2Config['grant']>>(
    'client_credentials'
  );

  useEffect(() => {
    if (!visible) return;
    form.resetFields();
    setAuthType(environment?.default_auth?.type || 'none');
    setAuthGrant(environment?.default_auth?.oauth2?.grant || 'client_credentials');
    form.setFieldsValue({
      variables: toEnvVariablesForm(environment?.variables),
      headers: toEnvHeadersForm(environment?.headers),
      // 031（US4）：多域名服务地址
      services_list: toEnvServicesForm(environment?.services),
      // 033（US3）：默认认证（摊平字段）
      ...toAuthForm(environment?.default_auth),
    });
  }, [visible, environment, form]);

  const handleSave = async () => {
    if (!environment) return;
    let values: EnvVariableFormShape;
    try {
      values = await form.validate();
    } catch {
      return;
    }
    setSaving(true);
    try {
      await apiPut(
        `/api/environments/${environment.id}`,
        {
          variables: serializeEnvVariables(values.variables),
          headers: serializeEnvHeaders(values.headers),
          // 031（US4）：服务列表 ↔ dict
          services: serializeEnvServices(values.services_list),
          // 033（US3）：默认认证
          default_auth: serializeDefaultAuth(values as unknown as Record<string, unknown>),
        },
        '环境变量已保存'
      );
      onSaved();
      onClose();
    } catch {
      /* apiRequest 已弹错误 */
    } finally {
      setSaving(false);
    }
  };

  const effectiveVariables = environment?.variables || [];

  return (
    <Modal
      visible={visible}
      onCancel={onClose}
      onOk={handleSave}
      confirmLoading={saving}
      title={`环境变量 - ${environment?.name ?? ''}`}
      className={styles.envModal}
      okText="保存"
      cancelText="取消"
      unmountOnExit
    >
      <div className={styles.envSectionTitle}>当前已生效的变量</div>
      {effectiveVariables.length === 0 ? (
        <div className={styles.envEmptyHint}>该环境暂无变量</div>
      ) : (
        <Table
          className={styles.envReadonlyTable}
          columns={[
            { title: '变量名', dataIndex: 'key', width: 160 },
            { title: '变量值', render: (_: unknown, record: EnvVariableItem) => displayValue(record) },
            {
              title: '属性',
              width: 90,
              render: (_: unknown, record: EnvVariableItem) =>
                record.secret ? <Tag color="orange">加密</Tag> : <Tag>普通</Tag>,
            },
            {
              title: '状态',
              width: 80,
              render: (_: unknown, record: EnvVariableItem) =>
                record.enable === false ? <Tag color="gray">停用</Tag> : <Tag color="green">启用</Tag>,
            },
          ]}
          data={effectiveVariables}
          rowKey="key"
          pagination={false}
          size="small"
        />
      )}
      <Text className={styles.envHint}>
        执行用例时 scope=environment 的提取器会把获取到的变量写回当前环境，执行后刷新本页即可看到。
      </Text>

      <div className={styles.envSectionTitle}>变量配置</div>
      <Form form={form} layout="vertical">
        <EnvVariablesList form={form} t={T} />
        <EnvServicesList form={form} t={T} />

        {/* 033（US3）：默认认证 —— 用例步骤 auth=inherit 且未命中用例/场景层时使用 */}
        <div className={styles.envSectionTitle}>默认认证（可选）</div>
        <Form.Item label="认证类型" field="auth_type">
          <Select
            onChange={(v) => setAuthType((v as AuthConfig['type']) || 'none')}
            options={[
              { label: '不配置（none）', value: 'none' },
              { label: 'Basic', value: 'basic' },
              { label: 'Bearer Token', value: 'bearer' },
              { label: 'API Key', value: 'api_key' },
              { label: 'OAuth2', value: 'oauth2' },
            ]}
          />
        </Form.Item>
        {authType === 'basic' && (
          <>
            <Form.Item label="用户名" field="auth_username">
              <Input />
            </Form.Item>
            <Form.Item label="密码" field="auth_password">
              <Input.Password placeholder="保持 ****** 表示不修改原值" />
            </Form.Item>
          </>
        )}
        {authType === 'bearer' && (
          <Form.Item label="Token" field="auth_token">
            <Input.Password placeholder="保持 ****** 表示不修改原值" />
          </Form.Item>
        )}
        {authType === 'api_key' && (
          <>
            <Form.Item label="Key 名" field="auth_key_name">
              <Input placeholder="如 X-API-Key" />
            </Form.Item>
            <Form.Item label="Key 值" field="auth_key_value">
              <Input.Password placeholder="保持 ****** 表示不修改原值" />
            </Form.Item>
            <Form.Item label="传递位置" field="auth_in_">
              <Select
                options={[
                  { label: '请求头（header）', value: 'header' },
                  { label: '查询参数（query）', value: 'query' },
                ]}
              />
            </Form.Item>
          </>
        )}
        {authType === 'oauth2' && (
          <>
            <Form.Item label="授权流" field="auth_grant">
              <Select
                onChange={(v) => setAuthGrant(v as NonNullable<AuthOAuth2Config['grant']>)}
                options={[
                  { label: 'client_credentials（客户端凭据）', value: 'client_credentials' },
                  { label: 'password（密码）', value: 'password' },
                  { label: 'refresh_token（刷新令牌）', value: 'refresh_token' },
                ]}
              />
            </Form.Item>
            <Form.Item
              label="Token URL"
              field="auth_token_url"
              rules={[{ required: true, message: '请输入 Token URL' }]}
            >
              <Input placeholder="https://idp.example.com/oauth/token" />
            </Form.Item>
            <Form.Item
              label="Client ID"
              field="auth_client_id"
              rules={[{ required: true, message: '请输入 Client ID' }]}
            >
              <Input />
            </Form.Item>
            <Form.Item label="Client Secret" field="auth_client_secret">
              <Input.Password placeholder="保持 ****** 表示不修改原值" />
            </Form.Item>
            <Form.Item label="Scope" field="auth_scope">
              <Input placeholder="可选，如 read write" />
            </Form.Item>
            {authGrant === 'password' && (
              <>
                <Form.Item label="用户名" field="auth_oauth_username">
                  <Input />
                </Form.Item>
                <Form.Item label="密码" field="auth_oauth_password">
                  <Input.Password />
                </Form.Item>
              </>
            )}
            {authGrant === 'refresh_token' && (
              <Form.Item label="Refresh Token" field="auth_refresh_token">
                <Input.Password placeholder="保持 ****** 表示不修改原值" />
              </Form.Item>
            )}
            <Form.Item label="客户端凭据传递" field="auth_client_auth">
              <Select
                options={[
                  { label: '请求体（body，默认）', value: 'body' },
                  { label: 'Basic 认证头（basic）', value: 'basic' },
                ]}
              />
            </Form.Item>
            <Form.Item label="令牌变量名" field="auth_token_var">
              <Input placeholder="默认 access_token（写入运行时变量）" />
            </Form.Item>
          </>
        )}
        <Text className={styles.envHint}>
          用例步骤认证选「inherit」且用例/场景未显式配置时，将使用本环境默认认证。
        </Text>

        <div className={styles.envSectionTitle}>公共请求头</div>
        <EnvHeadersList t={T} />
      </Form>

      <Text className={styles.envHint}>
        {'{{变量}}'} 取值顺序：提取(运行时/环境) &gt; 步骤 &gt; 用例 &gt; 数据集 &gt; 环境 &gt; baseUrl；
        内置 {'{{$uuid}}'} {'{{$timestamp}}'} {'{{$randomInt}}'}
      </Text>
    </Modal>
  );
};

export default EnvVariablesModal;
