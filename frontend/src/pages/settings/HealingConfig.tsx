import React, { useEffect, useState } from 'react';
import { Card, Form, Switch, Slider, InputNumber, Button, Message, Spin, Space } from '@arco-design/web-react';
import { apiGet, apiPut } from '@/utils/apiRequest';

const HealingConfigPage: React.FC = () => {
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [enabled, setEnabled] = useState(true);
  const [maxRetries, setMaxRetries] = useState(3);
  const [threshold, setThreshold] = useState(0.8);

  useEffect(() => {
    apiGet<any>('/api/config/healing')
      .then((data) => {
        setEnabled(data.enabled ?? true);
        setMaxRetries(data.max_retries ?? 3);
        setThreshold(data.threshold ?? 0.8);
      })
      .catch(() => Message.error('加载自愈配置失败'))
      .finally(() => setLoading(false));
  }, []);

  const handleSave = async () => {
    setSaving(true);
    try {
      await apiPut('/api/config/healing', { enabled, max_retries: maxRetries, threshold });
      Message.success('自愈配置已更新');
    } catch {
      Message.error('保存失败');
    } finally {
      setSaving(false);
    }
  };

  if (loading) return <Spin loading className="spin-center" />;

  return (
    <Card title="自愈选择器配置">
      <Form layout="vertical" style={{ maxWidth: 500 }}>
        <Form.Item label="启用自愈">
          <Switch checked={enabled} onChange={setEnabled} />
        </Form.Item>
        <Form.Item label="最大重试次数">
          <InputNumber value={maxRetries} min={0} max={10} onChange={setMaxRetries} />
        </Form.Item>
        <Form.Item label="相似度阈值">
          <Space>
            <Slider value={threshold} min={0} max={1} step={0.05} onChange={setThreshold} style={{ width: 200 }} />
            <span>{(threshold * 100).toFixed(0)}%</span>
          </Space>
        </Form.Item>
        <Button type="primary" onClick={handleSave} loading={saving}>保存</Button>
      </Form>
    </Card>
  );
};

export default HealingConfigPage;
