import React from 'react';
import { Table, Button, Form, Input, Switch } from '@arco-design/web-react';
import { IconPlus, IconDelete } from '@arco-design/web-react/icon';
import styles from './envVariablesEditor.module.less';

/**
 * 可复用的「环境变量 / 公共请求头」表单列表。
 *
 * 从 pages/testcases/components/EnvironmentManager.tsx 提炼而来，供
 * 测试用例页环境编辑与接口测试页环境变量编辑器共用（避免两份实现）。
 *
 * 使用时必须渲染在 <Form> 内，由外层 Form 持有 `variables` / `headers` 字段，
 * 并负责把表单值序列化为后端 payload（secret 项回传 "******" 表示不修改）。
 */

export interface EnvVariableFormValue {
  key: string;
  value: string;
  secret: boolean;
  enable: boolean;
  /** 仅 UI：secret 项在库中是否真有值；不回传后端 */
  has_value: boolean;
}

export interface EnvHeaderFormValue {
  key: string;
  value: string;
  enable: boolean;
}

type FormInstance = ReturnType<typeof Form.useForm>[0];

// Arco Form.Item 支持 valuePropName 但类型未暴露，用宽松类型包装
const FormItem = Form.Item as React.FC<{
  children?: React.ReactNode;
  label?: React.ReactNode;
  field?: string;
  valuePropName?: string;
  initialValue?: unknown;
  rules?: unknown[];
  noStyle?: boolean;
  [key: string]: unknown;
}>;

interface ListField {
  key: number;
  field: string;
}

interface VariablesListProps {
  form: FormInstance;
  t: Record<string, string>;
}

/** 环境变量编辑表：key / value(secret 打码) / 加密 / 启用 / 删除 */
export const EnvVariablesList: React.FC<VariablesListProps> = ({ form, t }) => {
  const variables =
    (Form.useWatch('variables', form) as EnvVariableFormValue[] | undefined) || [];
  return (
    <Form.List field="variables">
      {(fields, { add, remove }) => (
        <div>
          <Table<ListField>
            columns={[
              {
                title: t['environment.variable.key'],
                width: 150,
                render: (_: unknown, field: ListField) => (
                  <FormItem
                    {...field}
                    field={`${field.field}.key`}
                    noStyle
                    rules={[{ required: true, message: t['environment.variable.key_required'] }]}
                  >
                    <Input placeholder={t['environment.variable.key_placeholder']} />
                  </FormItem>
                ),
              },
              {
                title: t['environment.variable.value'],
                render: (_: unknown, field: ListField, index: number) => {
                  const item = variables[index] || ({} as EnvVariableFormValue);
                  const placeholder = item.secret
                    ? (item.has_value
                      ? t['environment.variable.value_placeholder']
                      : t['environment.variable.not_set'])
                    : t['environment.variable.value_placeholder'];
                  return (
                    <FormItem {...field} field={`${field.field}.value`} noStyle>
                      <Input placeholder={placeholder} />
                    </FormItem>
                  );
                },
              },
              {
                title: t['environment.variable.secret'],
                width: 80,
                render: (_: unknown, field: ListField) => (
                  <FormItem {...field} field={`${field.field}.secret`} valuePropName="checked" noStyle>
                    <Switch size="small" />
                  </FormItem>
                ),
              },
              {
                title: t['environment.variable.enable'],
                width: 70,
                render: (_: unknown, field: ListField) => (
                  <FormItem {...field} field={`${field.field}.enable`} valuePropName="checked" noStyle>
                    <Switch size="small" />
                  </FormItem>
                ),
              },
              {
                title: t['actions'],
                width: 60,
                render: (_: unknown, field: ListField, index: number) => (
                  <Button
                    type="text"
                    size="mini"
                    status="danger"
                    icon={<IconDelete />}
                    onClick={() => remove(index)}
                    aria-label={t['environment.variable.remove']}
                  />
                ),
              },
            ]}
            data={fields as unknown as ListField[]}
            rowKey="key"
            pagination={false}
            size="small"
          />
          <div className={styles.hint}>{t['environment.variable.secret_hint']}</div>
          <Button
            type="dashed"
            long
            icon={<IconPlus />}
            onClick={() => add({ key: '', value: '', secret: false, enable: true, has_value: false })}
          >
            {t['environment.variable.add']}
          </Button>
        </div>
      )}
    </Form.List>
  );
};

/** 公共请求头编辑表：key / value / 启用 / 删除 */
export const EnvHeadersList: React.FC<{ t: Record<string, string> }> = ({ t }) => (
  <Form.List field="headers">
    {(fields, { add, remove }) => (
      <div>
        <Table<ListField>
          columns={[
            {
              title: t['environment.header.key'],
              width: 150,
              render: (_: unknown, field: ListField) => (
                <FormItem
                  {...field}
                  field={`${field.field}.key`}
                  noStyle
                  rules={[{ required: true, message: t['environment.header.key_required'] }]}
                >
                  <Input placeholder={t['environment.header.key_placeholder']} />
                </FormItem>
              ),
            },
            {
              title: t['environment.header.value'],
              render: (_: unknown, field: ListField) => (
                <FormItem {...field} field={`${field.field}.value`} noStyle>
                  <Input placeholder={t['environment.header.value_placeholder']} />
                </FormItem>
              ),
            },
            {
              title: t['environment.header.enable'],
              width: 70,
              render: (_: unknown, field: ListField) => (
                <FormItem {...field} field={`${field.field}.enable`} valuePropName="checked" noStyle>
                  <Switch size="small" />
                </FormItem>
              ),
            },
            {
              title: t['actions'],
              width: 60,
              render: (_: unknown, field: ListField, index: number) => (
                <Button
                  type="text"
                  size="mini"
                  status="danger"
                  icon={<IconDelete />}
                  onClick={() => remove(index)}
                  aria-label={t['environment.header.remove']}
                />
              ),
            },
          ]}
          data={fields as unknown as ListField[]}
          rowKey="key"
          pagination={false}
          size="small"
        />
        <Button
          type="dashed"
          long
          icon={<IconPlus />}
          onClick={() => add({ key: '', value: '', enable: true })}
        >
          {t['environment.header.add']}
        </Button>
      </div>
    )}
  </Form.List>
);

/** 031（US4）多域名服务表单项：{name, url} 列表 ↔ 后端 services dict */
export interface EnvServiceFormValue {
  name: string;
  url: string;
}

/**
 * 「服务地址」表（多域名环境）：模板中用 {{服务名}} 引用，优先级高于普通环境变量。
 * 渲染在 <Form> 内，字段名固定为 `services_list`（外层负责与 dict 互转）。
 */
export const EnvServicesList: React.FC<VariablesListProps> = ({ form, t }) => {
  const services =
    (Form.useWatch('services_list', form) as EnvServiceFormValue[] | undefined) || [];
  return (
    <Form.List field="services_list">
      {(fields, { add, remove }) => (
        <div>
          <Table<ListField>
            columns={[
              {
                title: t['environment.service.name'],
                width: 160,
                render: (_: unknown, field: ListField) => (
                  <FormItem
                    {...field}
                    field={`${field.field}.name`}
                    noStyle
                    rules={[{ required: true, message: t['environment.service.name_required'] }]}
                  >
                    <Input placeholder="auth" />
                  </FormItem>
                ),
              },
              {
                title: t['environment.service.url'],
                render: (_: unknown, field: ListField) => (
                  <FormItem
                    {...field}
                    field={`${field.field}.url`}
                    noStyle
                    rules={[
                      {
                        validator: (value: string | undefined, callback: (err?: string) => void) => {
                          const text = String(value || '').trim();
                          if (!text) return callback();
                          if (!/^https?:\/\//i.test(text)) {
                            return callback(t['environment.service.url_invalid']);
                          }
                          return callback();
                        },
                      },
                    ]}
                  >
                    <Input placeholder="http://auth.internal:8000" />
                  </FormItem>
                ),
              },
              {
                title: t['environment.variable.action'],
                width: 80,
                render: (_: unknown, field: ListField) => (
                  <Button
                    size="small"
                    type="text"
                    status="danger"
                    icon={<IconDelete />}
                    onClick={() => remove(field.key)}
                  />
                ),
              },
            ]}
            data={fields}
            rowKey="key"
            pagination={false}
            size="small"
          />
          <Button
            size="small"
            type="text"
            icon={<IconPlus />}
            onClick={() => add({ name: '', url: '' })}
            style={{ marginTop: 8 }}
          >
            {t['environment.service.add']}
          </Button>
          <div className={styles.hint || ''} style={{ marginTop: 6, fontSize: 12, color: 'var(--vt-text-3)' }}>
            {t['environment.service.hint']}
            {services.length > 0 && (
              <>
                ：{services.filter((s) => s?.name).map((s) => `{{${s.name}}}`).join('、')}
              </>
            )}
          </div>
        </div>
      )}
    </Form.List>
  );
};
