import React, { useEffect, useState } from 'react';
import { Form, Modal, Table, Tag, Typography } from '@arco-design/web-react';
import { apiPut } from '@/utils/apiRequest';
import { EnvVariablesList, EnvHeadersList } from '@/components/EnvVariablesEditor';
import {
  serializeEnvHeaders,
  serializeEnvVariables,
  toEnvHeadersForm,
  toEnvVariablesForm,
} from '@/pages/testcases/utils/envPayload';
import { EnvVariableItem, Environment } from '../types';
import styles from '../style/index.module.less';

const { Text } = Typography;

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
}

const T: Record<string, string> = {
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

  useEffect(() => {
    if (!visible) return;
    form.resetFields();
    form.setFieldsValue({
      variables: toEnvVariablesForm(environment?.variables),
      headers: toEnvHeadersForm(environment?.headers),
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
