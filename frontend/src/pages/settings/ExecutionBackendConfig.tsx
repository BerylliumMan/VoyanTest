import React, { useEffect, useState } from 'react';
import {
  Card,
  Form,
  Switch,
  InputNumber,
  Button,
  Message,
  Spin,
  Typography,
  Alert,
} from '@arco-design/web-react';
import { apiGet, apiPut } from '@/utils/apiRequest';

interface ExecutionBackendConfig {
  backend: 'ota';
  max_steps_per_nl: number;
  headless: boolean;
  keep_browser_after_run: boolean;
}

const ExecutionBackendConfigPage: React.FC = () => {
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [maxSteps, setMaxSteps] = useState(40);
  const [headless, setHeadless] = useState(true);
  const [keepBrowser, setKeepBrowser] = useState(true);

  useEffect(() => {
    apiGet<ExecutionBackendConfig>('/api/config/execution-backend')
      .then((data) => {
        setMaxSteps(data.max_steps_per_nl ?? 40);
        setHeadless(data.headless ?? true);
        setKeepBrowser(data.keep_browser_after_run ?? true);
      })
      .catch(() => Message.error('加载执行后端配置失败'))
      .finally(() => setLoading(false));
  }, []);

  const handleSave = async () => {
    setSaving(true);
    try {
      await apiPut('/api/config/execution-backend', {
        backend: 'ota',
        max_steps_per_nl: maxSteps,
        headless,
        keep_browser_after_run: keepBrowser,
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
        content="仅支持智能 OTA：AI 自主观察→决策→操作；成功后自动固化 Playwright 脚本，下次优先秒级回放，失败自动回退 OTA。"
      />
      <Form layout="vertical" style={{ maxWidth: 640 }}>
        <Form.Item label="执行引擎">
          <Typography.Text>智能 OTA（唯一）</Typography.Text>
        </Form.Item>
        <Form.Item label="OTA 最大轮数">
          <InputNumber
            value={maxSteps}
            min={3}
            max={80}
            onChange={(v) => setMaxSteps(Number(v) || 40)}
          />
          <Typography.Text type="secondary" style={{ marginLeft: 8 }}>
            观察→决策→操作 的最大循环次数
          </Typography.Text>
        </Form.Item>
        <Form.Item label="无头模式（仅服务端执行）">
          <Switch checked={headless} onChange={setHeadless} />
        </Form.Item>
        <Form.Item label="执行完成后保持浏览器打开">
          <Switch checked={keepBrowser} onChange={setKeepBrowser} />
          <Typography.Text type="secondary" style={{ marginLeft: 8 }}>
            关闭后：成功自动关闭浏览器，失败仍保留现场；开启则成功也保留，便于查看结果页
          </Typography.Text>
        </Form.Item>
        <Button type="primary" onClick={handleSave} loading={saving}>
          保存
        </Button>
      </Form>
    </Card>
  );
};

export default ExecutionBackendConfigPage;
