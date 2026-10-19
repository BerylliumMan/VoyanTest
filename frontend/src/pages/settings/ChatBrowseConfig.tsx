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

interface ChatBrowseRuntimeConfig {
  enabled: boolean;
  max_steps: number;
  timeout_s: number;
  vision: boolean;
  captcha_enabled: boolean;
  js_probe: boolean;
  context_max_chars: number;
  context_keep_messages: number;
}

const ChatBrowseConfigPage: React.FC = () => {
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [enabled, setEnabled] = useState(true);
  const [maxSteps, setMaxSteps] = useState(22);
  const [timeoutS, setTimeoutS] = useState(300);
  const [vision, setVision] = useState(true);
  const [captcha, setCaptcha] = useState(true);
  const [jsProbe, setJsProbe] = useState(true);
  const [contextMax, setContextMax] = useState(24000);
  const [contextKeep, setContextKeep] = useState(12);

  useEffect(() => {
    apiGet<ChatBrowseRuntimeConfig>('/api/config/chat-browse')
      .then((data) => {
        setEnabled(data.enabled ?? true);
        setMaxSteps(data.max_steps ?? 22);
        setTimeoutS(data.timeout_s ?? 300);
        setVision(data.vision ?? true);
        setCaptcha(data.captcha_enabled ?? true);
        setJsProbe(data.js_probe ?? true);
        setContextMax(data.context_max_chars ?? 24000);
        setContextKeep(data.context_keep_messages ?? 12);
      })
      .catch(() => Message.error('加载对话与探索配置失败'))
      .finally(() => setLoading(false));
  }, []);

  const handleSave = async () => {
    setSaving(true);
    try {
      await apiPut('/api/config/chat-browse', {
        enabled,
        max_steps: maxSteps,
        timeout_s: timeoutS,
        vision,
        captcha_enabled: captcha,
        js_probe: jsProbe,
        context_max_chars: contextMax,
        context_keep_messages: contextKeep,
      });
      Message.success('对话与探索配置已更新');
    } catch {
      Message.error('保存失败');
    } finally {
      setSaving(false);
    }
  };

  if (loading) return <Spin loading className="spin-center" />;

  return (
    <Card title="对话与探索">
      <Alert
        type="info"
        style={{ marginBottom: 16 }}
        content="浏览器探索（智能体对话中「打开浏览器探索/按描述操作」）的执行参数，以及对话上下文超限时的压缩策略。保存后立即生效。"
      />
      <Form layout="vertical" style={{ maxWidth: 640 }}>
        <Form.Item label="浏览器探索能力">
          <Switch checked={enabled} onChange={setEnabled} />
          <Typography.Text type="secondary" style={{ marginLeft: 8 }}>
            关闭后对话中的浏览器探索将不可用
          </Typography.Text>
        </Form.Item>
        <Form.Item label="探索最大步数">
          <InputNumber
            value={maxSteps}
            min={5}
            max={60}
            onChange={(v) => setMaxSteps(Number(v) || 22)}
          />
          <Typography.Text type="secondary" style={{ marginLeft: 8 }}>
            达到步数上限会提前结束探索（如实记录「达到步数上限」）
          </Typography.Text>
        </Form.Item>
        <Form.Item label="探索时间上限（秒）">
          <InputNumber
            value={timeoutS}
            min={60}
            max={1800}
            step={30}
            onChange={(v) => setTimeoutS(Number(v) || 300)}
          />
          <Typography.Text type="secondary" style={{ marginLeft: 8 }}>
            达到时间上限会提前结束探索（如实记录「达到时间上限」）
          </Typography.Text>
        </Form.Item>
        <Form.Item label="探索时读取页面截图（视觉）">
          <Switch checked={vision} onChange={setVision} />
          <Typography.Text type="secondary" style={{ marginLeft: 8 }}>
            每轮决策附页面截图，帮助识别弹窗/验证码/布局
          </Typography.Text>
        </Form.Item>
        <Form.Item label="验证码自动识别">
          <Switch checked={captcha} onChange={setCaptcha} />
          <Typography.Text type="secondary" style={{ marginLeft: 8 }}>
            登录页含图形验证码时自动识别并填写
          </Typography.Text>
        </Form.Item>
        <Form.Item label="只读探针（js_probe）">
          <Switch checked={jsProbe} onChange={setJsProbe} />
          <Typography.Text type="secondary" style={{ marginLeft: 8 }}>
            只读采集页面链接/存储键名等结构信息（不改页面状态）
          </Typography.Text>
        </Form.Item>
        <Form.Item label="对话上下文预算（字符）">
          <InputNumber
            value={contextMax}
            min={6000}
            max={200000}
            step={2000}
            onChange={(v) => setContextMax(Number(v) || 24000)}
          />
          <Typography.Text type="secondary" style={{ marginLeft: 8 }}>
            超出预算时较早对话自动摘要压缩；仍超限则截断较早消息
          </Typography.Text>
        </Form.Item>
        <Form.Item label="压缩时保留最近消息数">
          <InputNumber
            value={contextKeep}
            min={4}
            max={50}
            onChange={(v) => setContextKeep(Number(v) || 12)}
          />
        </Form.Item>
        <Button type="primary" onClick={handleSave} loading={saving}>
          保存
        </Button>
      </Form>
    </Card>
  );
};

export default ChatBrowseConfigPage;
