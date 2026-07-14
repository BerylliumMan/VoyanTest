import React, { useEffect, useState } from 'react';
import { Card, Form, Input, Button, Message, Spin, Space } from '@arco-design/web-react';
import axios from 'axios';
import useLocale from '@/utils/useLocale';
import styles from './style/index.module.less';

function AiConfig() {
  const t = useLocale();
  const [loading, setLoading] = useState(false);
  const [form] = Form.useForm();
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);

  useEffect(() => {
    setLoading(true);
    axios
      .get('/api/config/ai')
      .then((res) => form.setFieldsValue(res.data))
      .catch((err) => Message.error(err?.response?.data?.detail || t['operate.failed']))
      .finally(() => setLoading(false));
  }, []);

  const handleSubmit = async (values: Record<string, unknown>) => {
    setSaving(true);
    try {
      await axios.put('/api/config/ai', {
        ...values,
        temperature: Number(values.temperature),
        api_key: values.api_key || undefined,
      });
      Message.success(t['save.success']);
    } catch (e: unknown) {
      const err = e as { response?: { data?: { detail?: string } } };
      Message.error(err.response?.data?.detail || t['save.failed']);
    } finally {
      setSaving(false);
    }
  };

  const handleTest = async () => {
    const values = form.getFields();
    setTesting(true);
    try {
      const res = await axios.post('/api/config/ai/test', {
        model: values.model || undefined,
        api_key: values.api_key || undefined,
        api_base: values.api_base || undefined,
      });
      Message.success(res.data?.message || '连接成功');
    } catch (e: unknown) {
      const err = e as { response?: { data?: { detail?: string } } };
      Message.error(err.response?.data?.detail || '连接测试失败');
    } finally {
      setTesting(false);
    }
  };

  return (
    <Card className={styles.fullWidth}>
      <Spin loading={loading} className={styles.fullWidth}>
        <Form form={form} onSubmit={handleSubmit} layout="vertical" className={styles.fullWidth}>
          <Form.Item field="model" label={t['model.name']} rules={[{ required: true }]}>
            <Input placeholder={t['model.name.placeholder']} />
          </Form.Item>
          <Form.Item field="api_base" label={t['api.url']} rules={[{ required: true }]}>
            <Input placeholder="https://api.openai.com/v1" />
          </Form.Item>
          <Form.Item field="api_key" label={t['api.key']}>
            <Input.Password placeholder={t['api.key.placeholder']} />
          </Form.Item>
          <Form.Item field="temperature" label={t['temperature']}>
            <Input type="number" step={0.1} min={0} max={2} />
          </Form.Item>
          <Form.Item>
            <Space>
              <Button type="primary" htmlType="submit" loading={saving}>
                {t['save.config']}
              </Button>
              <Button onClick={handleTest} loading={testing}>
                测试连接
              </Button>
            </Space>
          </Form.Item>
        </Form>
      </Spin>
    </Card>
  );
}

export default AiConfig;
