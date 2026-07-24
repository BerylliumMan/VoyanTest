import { useEffect, useState } from 'react';
import {
  Card,
  Button,
  Modal,
  Form,
  Input,
  Select,
  Switch,
  Tag,
  Space,
  Popconfirm,
  Message,
  Collapse,
  Slider,
  InputNumber,
  Grid,
  Spin,
} from '@arco-design/web-react';
import { IconPlus, IconEdit, IconDelete } from '@arco-design/web-react/icon';
import axios from 'axios';
import useLocale from '@/utils/useLocale';
import styles from './style/index.module.less';

const CollapseItem = Collapse.Item;
const { Row, Col } = Grid;

interface PromptOption {
  key: string;
  name: string;
  category: string;
  version: string;
  is_active: boolean;
}

interface AgentDefinition {
  id: number;
  name: string;
  agent_type: string;
  description: string;
  skills: string[];
  llm_config: Record<string, unknown>;
  prompt_overrides: Record<string, string>;
  system_prompt: string;
  is_active: boolean;
  created_at: string;
}

const AGENT_TYPE_COLORS: Record<string, string> = {
  generation: 'blue',
  execution: 'green',
  recording: 'orange',
};

const AGENT_TYPE_OPTIONS = [
  { label: 'Generation', value: 'generation' },
  { label: 'Execution', value: 'execution' },
  { label: 'Recording', value: 'recording' },
];

const PROVIDER_OPTIONS = [
  { label: 'OpenAI', value: 'openai' },
  { label: 'Azure', value: 'azure' },
  { label: 'Bedrock', value: 'bedrock' },
  { label: 'Ollama', value: 'ollama' },
  { label: 'Custom', value: 'custom' },
];

function AgentDefinitions() {
  const t = useLocale();
  const [data, setData] = useState<AgentDefinition[]>([]);
  const [loading, setLoading] = useState(false);
  const [visible, setVisible] = useState(false);
  const [editing, setEditing] = useState<AgentDefinition | null>(null);
  const [form] = Form.useForm();
  const [submitLoading, setSubmitLoading] = useState(false);
  const [promptOptions, setPromptOptions] = useState<PromptOption[]>([]);

  const fetchData = () => {
    setLoading(true);
    axios
      .get('/api/agent-definitions')
      .then((res) => setData(res.data || []))
      .catch((err) => Message.error(err?.response?.data?.detail || t['operate.failed']))
      .finally(() => setLoading(false));
  };

  useEffect(() => {
    fetchData();
    axios
      .get('/api/config/prompts')
      .then((res) => setPromptOptions(res.data || []))
      .catch(() => {
        /* silent */
      });
  }, []);

  const openModal = (record?: AgentDefinition) => {
    setEditing(record || null);
    form.resetFields();
    if (record) {
      const llm = record.llm_config || {};
      form.setFieldsValue({
        name: record.name,
        agent_type: record.agent_type,
        description: record.description,
        skills: record.skills || [],
        system_prompt: record.system_prompt || '',
        llm_model: llm.model || '',
        llm_temperature: llm.temperature ?? 0.7,
        llm_max_tokens: llm.max_tokens ?? 4096,
        llm_api_base: llm.api_base || '',
        llm_provider: llm.provider || 'openai',
        is_active: record.is_active,
      });
    }
    setVisible(true);
  };

  const buildLlmConfig = (values: Record<string, unknown>): Record<string, unknown> => ({
    model: values.llm_model,
    temperature: values.llm_temperature,
    max_tokens: values.llm_max_tokens,
    api_base: values.llm_api_base,
    provider: values.llm_provider,
  });

  const handleSubmit = async () => {
    const values = await form.validate();
    setSubmitLoading(true);

    const payload = {
      name: values.name,
      agent_type: values.agent_type,
      description: values.description || '',
      skills: values.skills || [],
      llm_config: buildLlmConfig(values),
      system_prompt: values.system_prompt || '',
      is_active: values.is_active || false,
    };

    try {
      if (editing) {
        await axios.put(`/api/agent-definitions/${editing.id}`, payload);
      } else {
        await axios.post('/api/agent-definitions', payload);
      }
      Message.success(t['save.success']);
      setVisible(false);
      fetchData();
    } catch (e: unknown) {
      const err = e as { response?: { data?: { detail?: string } } };
      Message.error(err?.response?.data?.detail || t['operate.failed']);
    } finally {
      setSubmitLoading(false);
    }
  };

  const handleToggleActive = async (record: AgentDefinition) => {
    try {
      await axios.put(`/api/agent-definitions/${record.id}`, {
        is_active: !record.is_active,
      });
      Message.success(t['save.success']);
      fetchData();
    } catch (e: unknown) {
      const err = e as { response?: { data?: { detail?: string } } };
      Message.error(err?.response?.data?.detail || t['operate.failed']);
    }
  };

  const handleDelete = async (record: AgentDefinition) => {
    try {
      await axios.delete(`/api/agent-definitions/${record.id}`);
      Message.success(t['save.success']);
      fetchData();
    } catch (e: unknown) {
      const err = e as { response?: { data?: { detail?: string } } };
      Message.error(err?.response?.data?.detail || t['operate.failed']);
    }
  };


  const getLlmConfig = (llm: Record<string, unknown> | null | undefined) => {
    if (!llm) return { model: '', temperature: 0, api_base: '', provider: '' };
    return {
      model: String(llm.model || ''),
      temperature: Number(llm.temperature ?? 0),
      api_base: String(llm.api_base || ''),
      provider: String(llm.provider || ''),
    };
  };

  return (
    <Card title={t['agent_definitions.list.title']}>
      <div className={styles.actionsRow}>
        <Button type="primary" icon={<IconPlus />} onClick={() => openModal()}>
          {t['agent_definitions.list.create']}
        </Button>
      </div>
      {loading ? (
        <div className={styles.loadingContainer}>
          <Spin size={40} />
        </div>
      ) : data.length === 0 ? (
        <div className={styles.emptyState}>
          <span className={styles.emptyText}>No agents configured</span>
        </div>
      ) : (
        <Row gutter={[16, 16]}>
          {data.map((agent) => {
            const llmCfg = getLlmConfig(agent.llm_config);
            return (
              <Col xs={24} sm={12} lg={8} key={agent.id}>
                <Card
                  className={`${styles.agentCard}${agent.is_active ? ` ${styles.activeCard}` : ''}`}
                >
                  <div className={styles.cardHeader}>
                    <div className={styles.cardTitle}>
                      <span className={styles.agentName}>{agent.name}</span>
                      <Tag color={AGENT_TYPE_COLORS[agent.agent_type] || 'gray'}>
                        {agent.agent_type}
                      </Tag>
                    </div>
                    <Switch
                      checked={agent.is_active}
                      onChange={() => handleToggleActive(agent)}
                    />
                  </div>

                  <div className={styles.cardBody}>
                    <div className={styles.cardSection}>
                      <p className={styles.description}>
                        {agent.description || 'No description'}
                      </p>
                    </div>

                    <div className={styles.cardSection}>
                      {agent.skills && agent.skills.length > 0 ? (
                        <Space wrap size="small">
                          {agent.skills.map((skill) => (
                            <Tag
                              key={skill}
                              size="small"
                              color={
                                agent.prompt_overrides?.[skill] ? 'green' : undefined
                              }
                            >
                              {skill}
                              {agent.prompt_overrides?.[skill] ? ' *' : ''}
                            </Tag>
                          ))}
                        </Space>
                      ) : (
                        <span className={styles.mutedText}>No skills</span>
                      )}
                    </div>

                    <div className={styles.cardSection}>
                      <span className={styles.llmInfo}>
                        {llmCfg.model || '-'}
                        {llmCfg.temperature !== 0 &&
                          ` · temp: ${llmCfg.temperature}`}
                      </span>
                    </div>

                    <div className={styles.cardSection}>
                      <span className={styles.configMeta}>
                        {llmCfg.api_base
                          ? `${llmCfg.api_base.length > 40 ? `${llmCfg.api_base.slice(0, 40)}...` : llmCfg.api_base}`
                          : '-'}{' '}
                        · {llmCfg.provider || '-'}
                      </span>
                    </div>
                  </div>

                  <div className={styles.cardFooter}>
                    <span className={styles.createdAt}>
                      {agent.created_at
                        ? new Date(agent.created_at).toLocaleDateString()
                        : '-'}
                    </span>
                    <Space>
                      <Button
                        type="text"
                        size="small"
                        icon={<IconEdit />}
                        onClick={() => openModal(agent)}
                      >
                        {t['edit']}
                      </Button>
                      <Popconfirm
                        title={`Delete agent "${agent.name}"?`}
                        onOk={() => handleDelete(agent)}
                      >
                        <Button
                          type="text"
                          size="small"
                          icon={<IconDelete />}
                        >
                          {t['delete']}
                        </Button>
                      </Popconfirm>
                    </Space>
                  </div>
                </Card>
              </Col>
            );
          })}
        </Row>
      )}

      <Modal
        visible={visible}
        onCancel={() => setVisible(false)}
        title={
          editing
            ? `Edit ${t['agent_definitions.list.title']}`
            : `Create ${t['agent_definitions.list.title']}`
        }
        onOk={handleSubmit}
        confirmLoading={submitLoading}
        style={{ minWidth: 600 }}
      >
        <Form form={form} layout="vertical">
          <Form.Item
            field="name"
            label={t['agent_definitions.form.name']}
            rules={[{ required: true }]}
          >
            <Input placeholder={t['agent_definitions.form.name']} />
          </Form.Item>

          <Form.Item
            field="agent_type"
            label={t['agent_definitions.form.agent_type']}
            rules={[{ required: true }]}
          >
            <Select disabled={!!editing} options={AGENT_TYPE_OPTIONS} />
          </Form.Item>

          <Form.Item field="description" label={t['agent_definitions.form.description']}>
            <Input.TextArea placeholder={t['agent_definitions.form.description']} />
          </Form.Item>

          <Form.Item field="skills" label={t['agent_definitions.form.skills']}>
            <Select
              mode="multiple"
              placeholder={t['agent_definitions.form.skills']}
              options={promptOptions.map((p) => ({
                label: `${p.name} (${p.key})`,
                value: p.key,
              }))}
            />
          </Form.Item>

          <div className={styles.sectionTitle}>
            {t['agent_definitions.form.llm_config']}
          </div>

          <Form.Item field="llm_model" label="Model" rules={[{ required: true }]}>
            <Input placeholder="gpt-4" />
          </Form.Item>

          <Form.Item field="llm_temperature" label="Temperature">
            <Slider min={0} max={2} step={0.1} />
          </Form.Item>

          <Form.Item field="llm_max_tokens" label="Max Tokens">
            <InputNumber min={1} max={128000} style={{ width: '100%' }} />
          </Form.Item>

          <Collapse className={styles.advancedSection}>
            <CollapseItem header="Advanced" name="advanced">
              <Form.Item field="llm_api_base" label="API Base">
                <Input placeholder="https://api.openai.com/v1" />
              </Form.Item>
              <Form.Item field="llm_provider" label="Provider">
                <Select options={PROVIDER_OPTIONS} />
              </Form.Item>
            </CollapseItem>
          </Collapse>

          <Form.Item
            field="system_prompt"
            label={t['agent_definitions.form.system_prompt']}
          >
            <Input.TextArea
              rows={4}
              autoSize
              placeholder={t['agent_definitions.form.system_prompt_placeholder']}
            />
          </Form.Item>

          <Form.Item
            field="is_active"
            label={t['agent_definitions.form.is_active']}
            triggerPropName="checked"
          >
            <Switch />
          </Form.Item>
          <div className={styles.activeHint}>
            {t['agent_definitions.form.confirm_activate']}
          </div>
        </Form>
      </Modal>
    </Card>
  );
}

export default AgentDefinitions;
