import React, { useEffect, useState } from 'react';
import { Card, Form, Switch, Slider, InputNumber, Button, Message, Spin, Space, Select } from '@arco-design/web-react';
import { apiGet, apiPut } from '@/utils/apiRequest';

type MemoryMode = 'read_write' | 'read_only' | 'off';

const HealingConfigPage: React.FC = () => {
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [enabled, setEnabled] = useState(true);
  const [maxRetries, setMaxRetries] = useState(3);
  const [threshold, setThreshold] = useState(0.8);
  const [locatorMemory, setLocatorMemory] = useState(true);
  const [memoryMode, setMemoryMode] = useState<MemoryMode>('read_write');
  const [previewEnabled, setPreviewEnabled] = useState(false);

  useEffect(() => {
    apiGet<any>('/api/config/healing')
      .then((data) => {
        setEnabled(data.enabled ?? true);
        setMaxRetries(data.max_retries ?? 3);
        setThreshold(data.threshold ?? 0.8);
        setLocatorMemory(data.locator_memory_enabled ?? true);
        setMemoryMode((data.locator_memory_mode as MemoryMode) || 'read_write');
        setPreviewEnabled(Boolean(data.locator_preview_enabled));
      })
      .catch(() => Message.error('加载自愈配置失败'))
      .finally(() => setLoading(false));
  }, []);

  const handleSave = async () => {
    setSaving(true);
    try {
      const mode = locatorMemory ? memoryMode : 'off';
      await apiPut('/api/config/healing', {
        enabled,
        max_retries: maxRetries,
        threshold,
        locator_memory_enabled: locatorMemory && mode !== 'off',
        locator_memory_mode: mode,
        locator_preview_enabled: previewEnabled,
      });
      Message.success('自愈配置已更新');
    } catch {
      Message.error('保存失败');
    } finally {
      setSaving(false);
    }
  };

  if (loading) return <Spin loading className="spin-center" />;

  return (
    <Card title="自愈与定位记忆">
      <Form layout="vertical" style={{ maxWidth: 560 }}>
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
        <Form.Item
          label="步骤定位记忆"
          extra="成功步骤记住元素/多步计划，下次优先重放；断言失败会自动作废"
        >
          <Switch
            checked={locatorMemory}
            onChange={(v) => {
              setLocatorMemory(v);
              if (!v) setMemoryMode('off');
              else if (memoryMode === 'off') setMemoryMode('read_write');
            }}
          />
        </Form.Item>
        <Form.Item
          label="定位记忆模式"
          extra="读写=学习并重放；只读=仅重放不写；关闭=完全不用记忆"
        >
          <Select
            value={memoryMode}
            disabled={!locatorMemory}
            onChange={(v) => setMemoryMode(v as MemoryMode)}
            options={[
              { label: '读写 (read_write)', value: 'read_write' },
              { label: '只读 (read_only)', value: 'read_only' },
              { label: '关闭 (off)', value: 'off' },
            ]}
          />
        </Form.Item>
        <Form.Item
          label="执行预览（调试）"
          extra="开启后步骤结果附带 Intent/候选元素预览，便于排查理解偏差"
        >
          <Switch checked={previewEnabled} onChange={setPreviewEnabled} />
        </Form.Item>
        <Button type="primary" onClick={handleSave} loading={saving}>保存</Button>
      </Form>
    </Card>
  );
};

export default HealingConfigPage;
