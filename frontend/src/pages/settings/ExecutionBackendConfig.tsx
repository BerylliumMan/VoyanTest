import React, { useEffect, useState } from 'react';
import {
  Card,
  Form,
  Select,
  Switch,
  InputNumber,
  Button,
  Message,
  Spin,
  Typography,
  Alert,
} from '@arco-design/web-react';
import { apiGet, apiPut } from '@/utils/apiRequest';

type Backend =
  | 'nl_goal'
  | 'compiled_script'
  | 'legacy_hybrid'
  | 'legacy_mcp'
  | 'browser_use';

interface ExecutionBackendConfig {
  backend: Backend;
  max_steps_per_nl: number;
  headless: boolean;
}

const ExecutionBackendConfigPage: React.FC = () => {
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [backend, setBackend] = useState<Backend>('nl_goal');
  const [maxSteps, setMaxSteps] = useState(40);
  const [headless, setHeadless] = useState(true);

  useEffect(() => {
    apiGet<ExecutionBackendConfig>('/api/config/execution-backend')
      .then((data) => {
        const b = (data.backend as string) || 'nl_goal';
        // migrate old names shown in UI
        if (b === 'hybrid' || b === 'playwright_mcp') {
          setBackend('nl_goal');
        } else {
          setBackend(b as Backend);
        }
        setMaxSteps(data.max_steps_per_nl ?? 40);
        setHeadless(data.headless ?? true);
      })
      .catch(() => Message.error('加载执行后端配置失败'))
      .finally(() => setLoading(false));
  }, []);

  const handleSave = async () => {
    setSaving(true);
    try {
      await apiPut('/api/config/execution-backend', {
        backend,
        max_steps_per_nl: maxSteps,
        headless,
      });
      Message.success('执行后端配置已更新');
    } catch {
      Message.error('保存失败');
    } finally {
      setSaving(false);
    }
  };

  if (loading) return <Spin loading className="spin-center" />;

  const turnsHint =
    backend === 'nl_goal' || backend === 'browser_use' || backend === 'legacy_hybrid';

  return (
    <Card title="执行后端">
      <Alert
        type="info"
        style={{ marginBottom: 16 }}
        content="UI 客户端默认：自然语言目标（整案多轮观察→操作→成功后合成 Playwright 脚本）。已固化脚本会优先回放。旧版逐步 MCP/hybrid 仅作兼容。"
      />
      <Form layout="vertical" style={{ maxWidth: 640 }}>
        <Form.Item label="默认执行引擎" required>
          <Select
            value={backend}
            onChange={setBackend}
            options={[
              {
                label: '自然语言目标（推荐，对齐 Cursor：整案 NL → 固化脚本）',
                value: 'nl_goal',
              },
              {
                label: '仅固化脚本（失败即败，不回退 NL）',
                value: 'compiled_script',
              },
              {
                label: '旧版混合（逐步 MCP + 单步 browser-use 救场）',
                value: 'legacy_hybrid',
              },
              {
                label: '旧版 Playwright MCP（逐步快照绑定）',
                value: 'legacy_mcp',
              },
              {
                label: 'browser-use 整案 NL（过渡）',
                value: 'browser_use',
              },
            ]}
          />
        </Form.Item>
        <Form.Item label="NL / 目标循环最大轮数" disabled={!turnsHint}>
          <InputNumber
            value={maxSteps}
            min={3}
            max={80}
            onChange={(v) => setMaxSteps(Number(v) || 40)}
          />
          <Typography.Text type="secondary" style={{ marginLeft: 8 }}>
            nl_goal 整案轮数；browser-use / 旧版 hybrid 救场步也使用
          </Typography.Text>
        </Form.Item>
        <Form.Item label="无头模式（仅服务端执行）">
          <Switch checked={headless} onChange={setHeadless} />
        </Form.Item>
        <Button type="primary" onClick={handleSave} loading={saving}>
          保存
        </Button>
      </Form>
    </Card>
  );
};

export default ExecutionBackendConfigPage;
