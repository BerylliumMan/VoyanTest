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

type Backend = 'playwright_mcp' | 'browser_use' | 'hybrid';

interface ExecutionBackendConfig {
  backend: Backend;
  max_steps_per_nl: number;
  headless: boolean;
}

const ExecutionBackendConfigPage: React.FC = () => {
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [backend, setBackend] = useState<Backend>('playwright_mcp');
  const [maxSteps, setMaxSteps] = useState(30);
  const [headless, setHeadless] = useState(true);

  useEffect(() => {
    apiGet<ExecutionBackendConfig>('/api/config/execution-backend')
      .then((data) => {
        setBackend(data.backend ?? 'playwright_mcp');
        setMaxSteps(data.max_steps_per_nl ?? 30);
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

  const buStepsEnabled = backend === 'browser_use' || backend === 'hybrid';

  return (
    <Card title="执行后端">
      <Alert
        type="info"
        style={{ marginBottom: 16 }}
        content="影响服务端执行与客户端 Agent 的默认引擎（未单独指定 backend 时）。hybrid 仅客户端生效：MCP 默认，定位失败时同浏览器 CDP 挂 browser-use 救场。「无头模式」仅作用于服务端；客户端以本地设置为准。配置在内存中，服务重启后恢复默认 Playwright MCP。"
      />
      <Form layout="vertical" style={{ maxWidth: 560 }}>
        <Form.Item label="默认执行引擎" required>
          <Select
            value={backend}
            onChange={setBackend}
            options={[
              {
                label: 'Playwright MCP（逐步：快照 → LLM → 操作）',
                value: 'playwright_mcp',
              },
              {
                label: 'browser-use（自然语言多轮自主执行）',
                value: 'browser_use',
              },
              {
                label: '混合（MCP 默认，定位失败同浏览器 browser-use 救场）',
                value: 'hybrid',
              },
            ]}
          />
        </Form.Item>
        <Form.Item
          label="browser-use 每步最大轮数"
          disabled={!buStepsEnabled}
        >
          <InputNumber
            value={maxSteps}
            min={3}
            max={50}
            onChange={(v) => setMaxSteps(Number(v) || 30)}
          />
          <Typography.Text type="secondary" style={{ marginLeft: 8 }}>
            browser-use / hybrid 救场步生效
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
