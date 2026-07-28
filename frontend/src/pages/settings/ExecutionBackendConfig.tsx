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

type Backend = 'playwright_mcp' | 'browser_use';

interface ExecutionBackendConfig {
  backend: Backend;
  max_steps_per_nl: number;
  headless: boolean;
}

const ExecutionBackendConfigPage: React.FC = () => {
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [backend, setBackend] = useState<Backend>('playwright_mcp');
  const [maxSteps, setMaxSteps] = useState(20);
  const [headless, setHeadless] = useState(true);

  useEffect(() => {
    apiGet<ExecutionBackendConfig>('/api/config/execution-backend')
      .then((data) => {
        setBackend(data.backend ?? 'playwright_mcp');
        setMaxSteps(data.max_steps_per_nl ?? 20);
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

  return (
    <Card title="执行后端">
      <Alert
        type="info"
        style={{ marginBottom: 16 }}
        content="影响服务端执行与客户端 Agent（未单独指定 backend 时）。browser-use 需 Agent 已安装并声明该能力。配置在内存中，服务重启后恢复默认 Playwright MCP。"
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
            ]}
          />
        </Form.Item>
        <Form.Item
          label="browser-use 每步最大轮数"
          disabled={backend !== 'browser_use'}
        >
          <InputNumber
            value={maxSteps}
            min={3}
            max={50}
            onChange={(v) => setMaxSteps(Number(v) || 20)}
          />
          <Typography.Text type="secondary" style={{ marginLeft: 8 }}>
            仅 browser-use 生效
          </Typography.Text>
        </Form.Item>
        <Form.Item label="无头模式（browser-use / 服务端）">
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
