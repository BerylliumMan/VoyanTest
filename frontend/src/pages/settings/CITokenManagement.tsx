import React, { useCallback, useEffect, useState } from 'react';
import {
  Button,
  Form,
  Input,
  InputNumber,
  Message,
  Modal,
  Popconfirm,
  Select,
  Space,
  Table,
  Tag,
  Typography,
} from '@arco-design/web-react';
import { IconPlus, IconCopy } from '@arco-design/web-react/icon';
import { apiRequest } from '@/utils/apiRequest';
import useLocale from '@/utils/useLocale';
import styles from './style/token.module.less';

/**
 * 031（US8）：CI 触发令牌管理。
 *
 * 契约（contracts §1.6）：
 *  · 签发响应**唯一一次**返回明文（`vt_…`）—— 弹窗内一次性展示并提供复制，关闭后不可再取
 *  · 列表只显示前缀/项目范围/过期/最近使用；撤销后立即失效（CI 侧 401）
 *  · 令牌为 `Authorization: Bearer vt_…`，供 `voyan api run` 与流水线使用
 */
interface ApiTokenRow {
  id: number;
  name: string;
  token_prefix: string;
  project_id: number | null;
  expires_at: string | null;
  revoked_at: string | null;
  last_used_at: string | null;
  created_at: string | null;
}

interface ProjectOption {
  id: number;
  name: string;
}

const CITokenManagement: React.FC = () => {
  const t = useLocale();
  const [rows, setRows] = useState<ApiTokenRow[]>([]);
  const [loading, setLoading] = useState(false);
  const [projects, setProjects] = useState<ProjectOption[]>([]);
  const [createVisible, setCreateVisible] = useState(false);
  const [creating, setCreating] = useState(false);
  const [plaintext, setPlaintext] = useState<string | null>(null);
  const [form] = Form.useForm();

  const fetchRows = useCallback(async () => {
    setLoading(true);
    try {
      const data = await apiRequest<{ items: ApiTokenRow[] }>({
        method: 'get',
        url: '/api/api-test/tokens',
      });
      setRows(data.items || []);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchRows();
    apiRequest<ProjectOption[] | { items: ProjectOption[] }>({
      method: 'get',
      url: '/api/projects',
    })
      .then((data) => {
        const list = Array.isArray(data) ? data : (data as { items?: ProjectOption[] }).items || [];
        setProjects(list);
      })
      .catch(() => setProjects([]));
  }, [fetchRows]);

  const handleCreate = async () => {
    const values = await form.validate();
    setCreating(true);
    try {
      const data = await apiRequest<ApiTokenRow & { plaintext: string }>({
        method: 'post',
        url: '/api/api-test/tokens',
        data: {
          name: values.name,
          project_id: values.project_id ?? null,
          expires_in_days: values.expires_in_days ?? null,
        },
      });
      setCreateVisible(false);
      form.resetFields();
      setPlaintext(data.plaintext); // 唯一一次展示
      fetchRows();
    } finally {
      setCreating(false);
    }
  };

  const handleRevoke = async (id: number) => {
    await apiRequest({ method: 'delete', url: `/api/api-test/tokens/${id}` });
    Message.success(t['ci.token.revoked']);
    fetchRows();
  };

  const copyPlaintext = async () => {
    if (!plaintext) return;
    try {
      await navigator.clipboard.writeText(plaintext);
      Message.success(t['ci.token.copied']);
    } catch {
      Message.warning(t['ci.token.copy_manual']);
    }
  };

  return (
    <div className={styles.wrap}>
      <div className={styles.header}>
        <div>
          <div className={styles.title}>{t['ci.token.title']}</div>
          <div className={styles.hint}>{t['ci.token.hint']}</div>
        </div>
        <Button type="primary" icon={<IconPlus />} onClick={() => setCreateVisible(true)}>
          {t['ci.token.create']}
        </Button>
      </div>

      <Table<ApiTokenRow>
        rowKey="id"
        size="small"
        loading={loading}
        data={rows}
        pagination={false}
        columns={[
          { title: t['ci.token.name'], dataIndex: 'name' },
          {
            title: t['ci.token.prefix'],
            dataIndex: 'token_prefix',
            render: (v: string) => <Typography.Text code>{v}…</Typography.Text>,
          },
          {
            title: t['ci.token.scope'],
            dataIndex: 'project_id',
            render: (v: number | null) =>
              v == null
                ? t['ci.token.scope_all']
                : projects.find((p) => p.id === v)?.name || `#${v}`,
          },
          {
            title: t['ci.token.expires'],
            dataIndex: 'expires_at',
            render: (v: string | null) => (v ? v.slice(0, 19).replace('T', ' ') : t['ci.token.never']),
          },
          {
            title: t['ci.token.last_used'],
            dataIndex: 'last_used_at',
            render: (v: string | null) =>
              v ? v.slice(0, 19).replace('T', ' ') : t['ci.token.never_used'],
          },
          {
            title: t['ci.token.status'],
            dataIndex: 'revoked_at',
            render: (v: string | null) =>
              v ? <Tag color="gray">{t['ci.token.revoked']}</Tag> : <Tag color="green">{t['ci.token.active']}</Tag>,
          },
          {
            title: t['ci.token.action'],
            render: (_: unknown, row: ApiTokenRow) =>
              row.revoked_at ? null : (
                <Popconfirm title={t['ci.token.revoke_confirm']} onOk={() => handleRevoke(row.id)}>
                  <Button size="small" status="danger" type="text">
                    {t['ci.token.revoke']}
                  </Button>
                </Popconfirm>
              ),
          },
        ]}
      />

      <Modal
        title={t['ci.token.create']}
        visible={createVisible}
        confirmLoading={creating}
        onOk={handleCreate}
        onCancel={() => {
          setCreateVisible(false);
          form.resetFields();
        }}
      >
        <Form form={form} layout="vertical" initialValues={{ expires_in_days: 90 }}>
          <Form.Item field="name" label={t['ci.token.name']} rules={[{ required: true }]}>
            <Input placeholder={t['ci.token.name_placeholder']} />
          </Form.Item>
          <Form.Item field="project_id" label={t['ci.token.scope']}>
            <Select
              allowClear
              placeholder={t['ci.token.scope_all']}
              options={projects.map((p) => ({ label: p.name, value: p.id }))}
            />
          </Form.Item>
          <Form.Item
            field="expires_in_days"
            label={t['ci.token.expires_days']}
            extra={t['ci.token.expires_days_hint']}
          >
            <InputNumber min={1} max={3650} style={{ width: 160 }} />
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        title={t['ci.token.plaintext_title']}
        visible={!!plaintext}
        footer={
          <Space>
            <Button icon={<IconCopy />} onClick={copyPlaintext}>
              {t['ci.token.copy']}
            </Button>
            <Button type="primary" onClick={() => setPlaintext(null)}>
              {t['ci.token.saved_it']}
            </Button>
          </Space>
        }
        onCancel={() => setPlaintext(null)}
        maskClosable={false}
      >
        <div className={styles.warn}>{t['ci.token.plaintext_warn']}</div>
        <Typography.Text code copyable={false} className={styles.plaintext}>
          {plaintext}
        </Typography.Text>
        <div className={styles.hint}>
          {t['ci.token.usage_hint']}
          <br />
          <Typography.Text code>
            voyan api run --case 12 --env 20 --base-url … --token {plaintext?.slice(0, 8)}…
          </Typography.Text>
        </div>
      </Modal>
    </div>
  );
};

export default CITokenManagement;
