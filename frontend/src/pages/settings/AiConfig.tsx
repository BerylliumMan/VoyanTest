import React, { useEffect, useState } from 'react';
import { Card, Form, Input, Button, Message, Spin, Space, Tabs, Tag } from '@arco-design/web-react';
import axios from 'axios';
import useLocale from '@/utils/useLocale';
import PromptEditor from './PromptEditor';
import styles from './style/index.module.less';

/* 预设的提示词键与中文标签 */
const PRESET_PROMPT_KEYS: { key: string; label: string; category: string }[] = [
  { key: 'fp_extract', label: '功能点提取', category: 'generation' },
  { key: 'tc_generate', label: '功能用例生成', category: 'generation' },
  { key: 'tc_generate_ui', label: 'UI自动化用例生成', category: 'generation' },
  { key: 'operation_translate', label: '操作指令翻译', category: 'execution' },
  { key: 'verify_expected', label: '预期结果验证', category: 'verification' },
];

interface PromptListItem {
  key: string;
  name: string;
  category: string;
  version: number;
  is_active: boolean;
}

function AiConfig() {
  const t = useLocale();
  const [loading, setLoading] = useState(false);
  const [form] = Form.useForm();
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);

  /* 提示词管理 */
  const [prompts, setPrompts] = useState<PromptListItem[]>([]);
  const [promptsLoading, setPromptsLoading] = useState(false);
  const [selectedPromptKey, setSelectedPromptKey] = useState<string>('');

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

  /* 加载提示词列表 */
  const fetchPrompts = () => {
    setPromptsLoading(true);
    axios
      .get('/api/config/prompts')
      .then((res) => {
        setPrompts(res.data || []);
      })
      .catch(() => {
        /* 没有提示词 API 时回退到预设列表 */
        setPrompts([]);
      })
      .finally(() => setPromptsLoading(false));
  };

  /* 标签页切换 */
  const handleTabChange = (key: string) => {
    if (key === 'prompts') {
      fetchPrompts();
    }
  };

  /* 渲染提示词键的显示 */
  const renderPromptLabel = (key: string, name: string, isActive: boolean) => {
    return (
      <span>
        {name || key}
        {isActive ? (
          <Tag color="green" size="small" style={{ marginLeft: 8 }}>
            使用中
          </Tag>
        ) : null}
      </span>
    );
  };

  return (
    <Tabs defaultActiveTab="ai" onChange={handleTabChange}>
      <Tabs.TabPane key="ai" title="AI 配置">
        <Card className={styles.fullWidth}>
          <Spin loading={loading} className={styles.fullWidth}>
            <Form
              form={form}
              onSubmit={handleSubmit}
              layout="vertical"
              className={styles.fullWidth}
            >
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
              <Form.Item field="max_context_tokens" label="上下文窗口 (tokens)">
                <Input type="number" min={4096} max={1048576} placeholder="默认 131072" />
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
      </Tabs.TabPane>

      <Tabs.TabPane key="prompts" title="提示词管理">
        <Card>
          <Spin loading={promptsLoading} className={styles.fullWidth}>
            <div className={styles.promptManager}>
              {/* 左侧：提示词列表 */}
              <div className={styles.promptSidebar}>
                <div className={styles.promptSidebarTitle}>提示词模板</div>
                {(prompts.length > 0 ? prompts : PRESET_PROMPT_KEYS).map((item) => {
                  const promptKey =
                    'key' in item ? item.key : (item as { key: string }).key;
                  const promptInfo = prompts.find((p) => p.key === promptKey);
                  const label = promptInfo
                    ? promptInfo.name
                    : 'label' in item && item.label
                      ? (item as { label: string }).label
                      : promptKey;
                  const isActive = promptInfo?.is_active ?? false;

                  return (
                    <div
                      key={promptKey}
                      className={`${styles.promptKeyItem} ${
                        selectedPromptKey === promptKey ? styles.promptKeyItemActive : ''
                      }`}
                      onClick={() => setSelectedPromptKey(promptKey)}
                    >
                      {renderPromptLabel(promptKey, label, isActive)}
                    </div>
                  );
                })}
              </div>

              {/* 右侧：编辑器 */}
              <div className={styles.promptEditorArea}>
                <PromptEditor promptKey={selectedPromptKey} />
              </div>
            </div>
          </Spin>
        </Card>
      </Tabs.TabPane>
    </Tabs>
  );
}

export default AiConfig;
