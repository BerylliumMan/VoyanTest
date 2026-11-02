import React, { useCallback, useEffect, useState } from 'react';
import {
  Alert,
  Button,
  Divider,
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
import { IconRefresh, IconSave, IconThunderbolt } from '@arco-design/web-react/icon';
import { apiDelete, apiGet, apiPost } from '@/utils/apiRequest';

const { Text } = Typography;

export interface RepoBindingPanelProps {
  visible: boolean;
  projectId?: number | null;
  onClose: () => void;
}

interface Binding {
  id?: number;
  repo_url: string;
  branch: string;
  auth_type: 'https_token' | 'ssh_key';
  auth_secret?: string | null;
  has_secret?: boolean;
  sync_mode: 'manual' | 'scheduled';
  sync_interval_minutes: number;
  extractor: 'auto' | 'openapi' | 'spring_source';
  openapi_url?: string | null;
  source_root?: string | null;
  confirm_window_minutes?: number | null;
  full_sync_every?: number;
  last_sync_at?: string | null;
  last_commit?: string | null;
  last_error?: string | null;
}

interface SyncRecord {
  id: number;
  trigger: string;
  status: string;
  started_at?: string | null;
  finished_at?: string | null;
  added: number;
  updated: number;
  removed: number;
  error?: string | null;
  details?: Record<string, unknown>;
}

const EMPTY: Binding = {
  repo_url: '',
  branch: 'main',
  auth_type: 'https_token',
  auth_secret: '',
  sync_mode: 'scheduled',
  sync_interval_minutes: 30,
  extractor: 'auto',
  openapi_url: '',
  source_root: '',
  confirm_window_minutes: null,
  full_sync_every: 10,
};

const STATUS_COLOR: Record<string, string> = {
  success: 'green',
  no_change: 'gray',
  failed: 'red',
};

function RepoBindingPanel({ visible, projectId, onClose }: RepoBindingPanelProps) {
  const [form] = Form.useForm<Binding>();
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [syncing, setSyncing] = useState(false);
  const [bound, setBound] = useState(false);
  const [records, setRecords] = useState<SyncRecord[]>([]);
  const [recordStatus, setRecordStatus] = useState<string>('');
  const [testResult, setTestResult] = useState<{ ok: boolean; text: string } | null>(null);

  const loadRecords = useCallback(async () => {
    if (!projectId) return;
    try {
      const qs = recordStatus ? `&status=${recordStatus}` : '';
      const data = await apiGet<{ items: SyncRecord[] }>(
        `/api/api-test/repos/${projectId}/syncs?limit=20${qs}`,
      );
      setRecords(data?.items || []);
    } catch {
      setRecords([]);
    }
  }, [projectId, recordStatus]);

  const load = useCallback(async () => {
    if (!projectId) return;
    setLoading(true);
    try {
      const data = await apiGet<{ bound: boolean; binding: Binding | null }>(
        `/api/api-test/repos/${projectId}`,
      );
      setBound(Boolean(data?.bound));
      form.setFieldsValue({ ...EMPTY, ...(data?.binding || {}) });
      if (data?.binding?.last_error) {
        Message.warning(`上次同步失败：${data.binding.last_error}`);
      }
      await loadRecords();
    } catch (e) {
      Message.error('加载仓库绑定失败');
    } finally {
      setLoading(false);
    }
  }, [projectId, form, loadRecords]);

  useEffect(() => {
    if (visible && projectId) {
      load();
    }
  }, [visible, projectId, load]);

  const handleSave = async () => {
    if (!projectId) return;
    try {
      const values = await form.validate();
      setSaving(true);
      const payload = {
        project_id: projectId,
        repo_url: values.repo_url,
        branch: values.branch || 'main',
        auth_type: values.auth_type,
        auth_secret: values.auth_secret || undefined,
        sync_mode: values.sync_mode,
        sync_interval_minutes: values.sync_interval_minutes,
        extractor: values.extractor,
        openapi_url: values.openapi_url || null,
        source_root: values.source_root || null,
        confirm_window_minutes: values.confirm_window_minutes || null,
        full_sync_every: values.full_sync_every || 10,
      };
      await apiPost('/api/api-test/repos', payload);
      Message.success('仓库绑定已保存');
      await load();
    } catch (e: any) {
      if (e?.message) Message.error(String(e.message));
    } finally {
      setSaving(false);
    }
  };

  const handleTest = async () => {
    if (!projectId || !bound) return;
    setTesting(true);
    setTestResult(null);
    try {
      const data = await apiPost<{
        ok: boolean;
        error?: string;
        head?: string;
        branches?: string[];
        branch_exists?: boolean;
      }>(`/api/api-test/repos/${projectId}/test`, {});
      if (data?.ok) {
        const branchInfo =
          data.branch_exists === false ? '（当前配置分支不存在）' : '';
        setTestResult({
          ok: true,
          text: `连接成功，HEAD=${(data.head || '').slice(0, 12)}${branchInfo}`,
        });
      } else {
        setTestResult({ ok: false, text: data?.error || '连接失败' });
      }
    } catch (e: any) {
      setTestResult({ ok: false, text: e?.message || '连接失败' });
    } finally {
      setTesting(false);
    }
  };

  const handleSync = async () => {
    if (!projectId || !bound) return;
    setSyncing(true);
    try {
      const beforeId = records[0]?.id ?? -1;
      await apiPost(`/api/api-test/repos/${projectId}/sync`, {});
      Message.success('同步已开始');
      // 轮询同步记录：出现新记录即视为本次同步结束（最多 30 秒）
      for (let i = 0; i < 15; i += 1) {
        // eslint-disable-next-line no-await-in-loop
        await new Promise((resolve) => setTimeout(resolve, 2000));
        // eslint-disable-next-line no-await-in-loop
        const data = await apiGet<{ items: SyncRecord[] }>(
          `/api/api-test/repos/${projectId}/syncs?limit=10`,
        );
        const items = data?.items || [];
        setRecords(items);
        if ((items[0]?.id ?? -1) > beforeId) break;
      }
      await load();
    } catch (e: any) {
      Message.error(e?.message || '同步失败');
    } finally {
      setSyncing(false);
    }
  };

  const handleSyncAndCheck = async () => {
    if (!projectId || !bound) return;
    setSyncing(true);
    try {
      await apiPost(`/api/api-test/repos/${projectId}/sync?check=true`, {});
      Message.success('同步并检查变更已开始');
      await new Promise((resolve) => setTimeout(resolve, 6000));
      await loadRecords();
      Message.info('变更检查完成，请打开「变更计划」查看结果');
    } catch (e: any) {
      Message.error(e?.message || '同步失败');
    } finally {
      setSyncing(false);
    }
  };

  const handleCancelSync = async () => {
    if (!projectId) return;
    try {
      const data = await apiPost<{ requested: boolean; memory_hit: boolean }>(
        `/api/api-test/repos/${projectId}/sync/cancel`,
        {},
      );
      if (data?.requested || data?.memory_hit) {
        Message.success('已请求取消，等待当前步骤结束');
      } else {
        Message.info('当前没有运行中的同步');
      }
      await loadRecords();
    } catch (e: any) {
      Message.error(e?.message || '取消失败');
    }
  };

  const handleUnbind = async () => {
    if (!projectId) return;
    try {
      await apiDelete(`/api/api-test/repos/${projectId}`);
      Message.success('已解绑仓库');
      setBound(false);
      form.resetFields();
      setRecords([]);
      onClose();
    } catch (e: any) {
      Message.error(e?.message || '解绑失败');
    }
  };

  return (
    <Modal
      title="代码仓库绑定"
      visible={visible}
      onCancel={onClose}
      footer={null}
      style={{ width: 760 }}
      unmountOnExit
    >
      <Form form={form} layout="vertical" disabled={!projectId} initialValues={EMPTY}>
        <Form.Item label="仓库地址" field="repo_url" rules={[{ required: true, message: '请填写仓库地址' }]}>
          <Input placeholder="https://git.example.com/group/api.git 或 git@git.example.com:group/api.git" />
        </Form.Item>
        <Space size="large" style={{ width: '100%' }}>
          <Form.Item label="分支" field="branch" style={{ width: 200 }}>
            <Input placeholder="main" />
          </Form.Item>
          <Form.Item label="凭据类型" field="auth_type" style={{ width: 160 }}>
            <Select
              options={[
                { label: 'HTTPS Token', value: 'https_token' },
                { label: 'SSH 私钥', value: 'ssh_key' },
              ]}
            />
          </Form.Item>
          <Form.Item label="同步方式" field="sync_mode" style={{ width: 140 }}>
            <Select
              options={[
                { label: '定时', value: 'scheduled' },
                { label: '手动', value: 'manual' },
              ]}
            />
          </Form.Item>
          <Form.Item label="同步间隔（分钟）" field="sync_interval_minutes" style={{ width: 150 }}>
            <InputNumber min={5} max={1440} />
          </Form.Item>
        </Space>
        <Form.Item
          label="凭据"
          field="auth_secret"
          extra="HTTPS 可填 token 或 user:token；SSH 填私钥内容。留空表示不修改已保存的凭据。"
        >
          <Input.TextArea
            placeholder={bound ? '已保存（显示为 ******），留空不修改' : '粘贴 token 或私钥'}
            autoSize={{ minRows: 2, maxRows: 5 }}
          />
        </Form.Item>
        <Space size="large" style={{ width: '100%' }}>
          <Form.Item label="提取方式" field="extractor" style={{ width: 200 }}>
            <Select
              options={[
                { label: '自动（文档优先）', value: 'auto' },
                { label: '仅 OpenAPI', value: 'openapi' },
                { label: '仅 Spring 源码', value: 'spring_source' },
              ]}
            />
          </Form.Item>
          <Form.Item label="源码目录（可选）" field="source_root" style={{ width: 260 }}>
            <Input placeholder="如 user-service/src/main/java" />
          </Form.Item>
          <Form.Item label="OpenAPI 地址（可选）" field="openapi_url" style={{ width: 240 }}>
            <Input placeholder="https://.../v3/api-docs" />
          </Form.Item>
        </Space>
        <Space size="large" style={{ width: '100%' }}>
          <Form.Item
            label="确认窗口（分钟，留空继承全局 60）"
            field="confirm_window_minutes"
            style={{ width: 260 }}
          >
            <InputNumber min={5} max={1440} placeholder="继承全局" />
          </Form.Item>
          <Form.Item label="每 N 次增量后全量校准" field="full_sync_every" style={{ width: 220 }}>
            <InputNumber min={1} max={1000} />
          </Form.Item>
        </Space>
      </Form>

      <Space>
        <Button type="primary" icon={<IconSave />} loading={saving} onClick={handleSave} disabled={!projectId}>
          保存绑定
        </Button>
        <Button icon={<IconThunderbolt />} loading={testing} onClick={handleTest} disabled={!bound}>
          连通性测试
        </Button>
        <Button icon={<IconRefresh />} loading={syncing} onClick={handleSync} disabled={!bound}>
          立即同步
        </Button>
        <Button
          icon={<IconThunderbolt />}
          loading={syncing}
          onClick={handleSyncAndCheck}
          disabled={!bound}
        >
          同步并检查变更
        </Button>
        {syncing ? (
          <Button status="warning" onClick={handleCancelSync}>
            取消同步
          </Button>
        ) : null}
        <Popconfirm title="解绑后不再自动同步，确定？" onOk={handleUnbind}>
          <Button status="danger" disabled={!bound}>
            解绑仓库
          </Button>
        </Popconfirm>
      </Space>

      {testResult ? (
        <Alert
          style={{ marginTop: 12 }}
          type={testResult.ok ? 'success' : 'error'}
          content={testResult.text}
        />
      ) : null}

      <Divider orientation="left">最近同步记录</Divider>
      <Space style={{ marginBottom: 8 }}>
        <Select
          size="small"
          value={recordStatus}
          onChange={setRecordStatus}
          style={{ width: 130 }}
          options={[
            { label: '全部状态', value: '' },
            { label: '同步中', value: 'running' },
            { label: '成功', value: 'success' },
            { label: '无变化', value: 'no_change' },
            { label: '失败', value: 'failed' },
            { label: '已取消', value: 'cancelled' },
          ]}
        />
        <Button size="small" onClick={loadRecords}>
          刷新记录
        </Button>
      </Space>
      <Table
        size="small"
        loading={loading}
        data={records}
        rowKey="id"
        pagination={false}
        columns={[
          {
            title: '状态',
            dataIndex: 'status',
            width: 100,
            render: (value: string) => (
              <Tag color={STATUS_COLOR[value] || 'gray'}>{value}</Tag>
            ),
          },
          { title: '触发', dataIndex: 'trigger', width: 80 },
          {
            title: '变更',
            width: 150,
            render: (_: unknown, row: SyncRecord) =>
              `新增 ${row.added} / 更新 ${row.updated} / 下线 ${row.removed}`,
          },
          {
            title: '完成时间',
            dataIndex: 'finished_at',
            render: (value: string | null) => value || '—',
          },
          {
            title: '错误',
            dataIndex: 'error',
            ellipsis: true,
            render: (value: string | null) =>
              value ? <Text type="error">{value}</Text> : '—',
          },
        ]}
      />
    </Modal>
  );
}

export default RepoBindingPanel;
