import React from 'react';
import {
  Modal, Table, Button, Space, Popconfirm, Tag, Form, Input, Select, Switch, Collapse,
} from '@arco-design/web-react';
import { IconPlus, IconEdit, IconDelete, IconCheck } from '@arco-design/web-react/icon';
import { Environment } from '../types';
import styles from '../style/components.module.less';

type FormInstance = ReturnType<typeof Form.useForm>[0];

interface EnvironmentManagerProps {
  manageVisible: boolean;
  onCloseManage: () => void;
  onCreate: () => void;
  environments: Environment[];
  onEdit: (env: Environment) => void;
  onDelete: (id: number) => void;
  onSetDefault: (id: number) => void;
  t: Record<string, string>;
  formVisible: boolean;
  editingEnv: Environment | null;
  onCancelForm: () => void;
  onSubmitForm: () => void;
  form: FormInstance;
}

const EnvironmentManager: React.FC<EnvironmentManagerProps> = ({
  manageVisible, onCloseManage, onCreate, environments,
  onEdit, onDelete, onSetDefault, t,
  formVisible, editingEnv, onCancelForm, onSubmitForm, form,
}) => {
  return (
    <>
      {/* Environment Management Modal */}
      <Modal visible={manageVisible} onCancel={onCloseManage}
        title={t['environments']} footer={null} className={styles['env-modal']}
      >
        <div className={styles['env-actions']}>
          <Button size="small" type="primary" icon={<IconPlus />} onClick={onCreate}>{t['environment.new']}</Button>
        </div>
        <Table
          columns={[
            { title: t['environment.form.name'], dataIndex: 'name', width: 120 },
            { title: t['environment.form.base_url'], dataIndex: 'base_url', ellipsis: true },
            { title: t['environment.form.browser'], dataIndex: 'browser', width: 90 },
            {
              title: t['environment.is_default'], dataIndex: 'is_default', width: 80,
              render: (v: boolean) => v ? <Tag color="green">{t['yes']}</Tag> : null,
            },
            {
              title: t['actions'], width: 200,
              render: (_: unknown, record: Environment) => (
                <Space>
                  {!record.is_default && (
                    <Button size="mini" type="secondary" icon={<IconCheck />}
                      onClick={() => onSetDefault(record.id)}
                    >{t['environment.set_default']}</Button>
                  )}
                  <Button size="mini" type="text" icon={<IconEdit />} onClick={() => onEdit(record)} aria-label="编辑环境" />
                  <Popconfirm
                    title={t['environment.delete.confirm'].replace('{name}', record.name)}
                    onOk={() => onDelete(record.id)}
                  >
                    <Button size="mini" type="text" status="danger" icon={<IconDelete />} aria-label="删除环境" />
                  </Popconfirm>
                </Space>
              ),
            },
          ]}
          data={environments} rowKey="id" pagination={false}
        />
      </Modal>

      {/* Environment Form Modal */}
      <Modal visible={formVisible}
        onCancel={onCancelForm}
        title={editingEnv ? t['environment.edit'] : t['environment.new']}
        onOk={onSubmitForm} className={styles['env-form-modal']}
      >
        <Form form={form} layout="vertical">
          <Form.Item field="name" label={t['environment.form.name']}
            rules={[{ required: true, message: t['environment.name.placeholder'] }]}
          >
            <Input placeholder={t['environment.name.placeholder']} />
          </Form.Item>
          <Form.Item field="base_url" label={t['environment.form.base_url']}
            rules={[{ required: true, message: t['environment.base_url'] }]}
          >
            <Input placeholder="https://example.com" />
          </Form.Item>
          <Space size="large">
            <Form.Item field="browser" label={t['environment.form.browser']}>
              <Select options={[
                { label: 'Chromium', value: 'chromium' },
                { label: 'Firefox', value: 'firefox' },
                { label: 'WebKit', value: 'webkit' },
              ]} className={styles['browser-select']} />
            </Form.Item>
            <Form.Item field="headless" label={t['environment.form.headless']} initialValue={true} valuePropName="checked">
              <Switch />
            </Form.Item>
          </Space>
          <Collapse style={{ marginBottom: 0 }}>
            <Collapse.Item header="认证 Cookie" name="cookies">
              <Form.List field="cookies">
                {(fields, { add, remove }) => (
                  <div>
                    {fields.map((field, index) => (
                      <div key={field.key} style={{ display: 'flex', gap: 8, marginBottom: 8, alignItems: 'center' }}>
                        <Form.Item
                          {...field}
                          field={`${field.field}.name`}
                          rules={[{ required: true, message: '请输入 Cookie 名称' }]}
                          noStyle
                        >
                          <Input placeholder="Cookie 名称" style={{ flex: 1 }} />
                        </Form.Item>
                        <Form.Item
                          {...field}
                          field={`${field.field}.value`}
                          rules={[{ required: true, message: '请输入 Cookie 值' }]}
                          noStyle
                        >
                          <Input.Password placeholder="Cookie 值" style={{ flex: 1 }} showEyeButton />
                        </Form.Item>
                        <Form.Item
                          {...field}
                          field={`${field.field}.domain`}
                          noStyle
                        >
                          <Input placeholder="可不填" style={{ flex: 1 }} />
                        </Form.Item>
                        <Button
                          type="text"
                          status="danger"
                          icon={<IconDelete />}
                          onClick={() => remove(index)}
                          aria-label="删除 Cookie"
                        />
                      </div>
                    ))}
                    <Button
                      type="dashed"
                      long
                      icon={<IconPlus />}
                      onClick={() => add({ name: '', value: '', domain: '' })}
                    >
                      添加 Cookie
                    </Button>
                  </div>
                )}
              </Form.List>
            </Collapse.Item>
          </Collapse>
        </Form>
      </Modal>
    </>
  );
};

export default EnvironmentManager;
