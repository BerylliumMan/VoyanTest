import React, { useEffect, useState, useMemo } from 'react';
import {
  Button,
  Input,
  Space,
  Tag,
  Message,
  Spin,
  Modal,
  Descriptions,
} from '@arco-design/web-react';
import axios from 'axios';
import useLocale from '@/utils/useLocale';
import PromptVersionHistory from './PromptVersionHistory';
import styles from './style/index.module.less';

interface PromptData {
  id: number;
  key: string;
  name: string;
  category: string;
  content: string;
  variables: string[];
  version: number;
  is_active: boolean;
  description: string | null;
  created_at: string | null;
  updated_at: string | null;
}

interface Props {
  promptKey: string;
}

/* 提示词分类的中文标签 */
const CATEGORY_LABELS: Record<string, string> = {
  generation: '用例生成',
  execution: '执行操作',
  verification: '结果验证',
};

/* 对 {{var}} 占位符进行高亮渲染 */
function renderWithHighlights(text: string): React.ReactNode[] {
  const parts = text.split(/({{[^}]+}})/g);
  return parts.map((part, i) => {
    if (part.startsWith('{{') && part.endsWith('}}')) {
      return (
        <span key={i} className={styles.variableHighlight}>
          {part}
        </span>
      );
    }
    return <span key={i}>{part}</span>;
  });
}

function PromptEditor({ promptKey }: Props) {
  const t = useLocale();
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [data, setData] = useState<PromptData | null>(null);
  const [content, setContent] = useState('');
  const [description, setDescription] = useState('');

  /* 预览状态 */
  const [previewVisible, setPreviewVisible] = useState(false);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [previewText, setPreviewText] = useState('');

  /* 版本历史弹窗 */
  const [historyVisible, setHistoryVisible] = useState(false);

  const fetchPrompt = () => {
    if (!promptKey) return;
    setLoading(true);
    axios
      .get(`/api/config/prompts/${promptKey}`)
      .then((res) => {
        const d: PromptData = res.data;
        setData(d);
        setContent(d.content);
        setDescription(d.description || '');
      })
      .catch((err) => {
        Message.error(err?.response?.data?.detail || t['operate.failed']);
      })
      .finally(() => setLoading(false));
  };

  useEffect(() => {
    fetchPrompt();
  }, [promptKey]);

  const handleSave = async () => {
    setSaving(true);
    try {
      await axios.post('/api/config/prompts', {
        key: promptKey,
        name: data?.name,
        category: data?.category,
        content,
        variables: data?.variables || [],
        description,
      });
      Message.success(t['save.success'] || '保存成功');
      fetchPrompt();
    } catch (e: unknown) {
      const err = e as { response?: { data?: { detail?: string } } };
      Message.error(err.response?.data?.detail || t['save.failed'] || '保存失败');
    } finally {
      setSaving(false);
    }
  };

  const handlePreview = async () => {
    setPreviewVisible(true);
    setPreviewLoading(true);
    setPreviewText('');
    try {
      const res = await axios.get(`/api/config/prompts/${promptKey}/preview`, {
        params: { sample: 'true' },
      });
      setPreviewText(res.data?.rendered || res.data?.content || JSON.stringify(res.data));
    } catch (e: unknown) {
      const err = e as { response?: { data?: { detail?: string } } };
      Message.error(err.response?.data?.detail || '预览失败');
    } finally {
      setPreviewLoading(false);
    }
  };

  /* 提取的变量列表 */
  const extractedVars = useMemo(() => {
    if (!content) return [];
    const matches = content.match(/{{([^}]+)}}/g);
    if (!matches) return [];
    return Array.from(new Set(matches.map((m) => m.replace(/{{|}}/g, ''))));
  }, [content]);

  if (!promptKey) {
    return <div className={styles.promptEmpty}>请选择要编辑的提示词</div>;
  }

  return (
    <Spin loading={loading} className={styles.fullWidth}>
      <div className={styles.promptEditor}>
        {/* 基本信息 */}
        {data && (
          <Descriptions
            colon={false}
            column={2}
            size="small"
            className={styles.promptInfo}
            data={[
              { label: '名称', value: data.name },
              { label: '分类', value: CATEGORY_LABELS[data.category] || data.category },
              { label: '当前版本', value: `v${data.version}` },
              {
                label: '状态',
                value: (
                  <Tag color={data.is_active ? 'green' : 'gray'}>
                    {data.is_active ? '使用中' : '未激活'}
                  </Tag>
                ),
              },
            ]}
          />
        )}

        {/* 编辑区域 */}
        <Input.TextArea
          value={content}
          onChange={(v: string) => setContent(v)}
          rows={18}
          className={styles.promptContent}
          placeholder="输入提示词模板，使用 {{变量名}} 作为占位符"
        />

        {/* 变量提取展示 */}
        {extractedVars.length > 0 && (
          <div className={styles.variableList}>
            <span className={styles.variableLabel}>可替换变量：</span>
            {extractedVars.map((v) => (
              <Tag key={v} color="arcoblue" className={styles.variableTag}>
                {`{{${v}}}`}
              </Tag>
            ))}
          </div>
        )}

        {/* 描述 */}
        <Input.TextArea
          value={description}
          onChange={(v: string) => setDescription(v)}
          rows={2}
          placeholder="版本变更说明（可选）"
          className={styles.promptDesc}
        />

        {/* 操作按钮 */}
        <Space className={styles.promptActions}>
          <Button type="primary" onClick={handleSave} loading={saving}>
            保存
          </Button>
          <Button onClick={handlePreview}>预览</Button>
          <Button onClick={() => setHistoryVisible(true)}>版本历史</Button>
        </Space>

        {/* 内容预览区域 —— 变量高亮 */}
        <div className={styles.promptPreviewBox}>
          <div className={styles.promptPreviewLabel}>内容预览（变量高亮）：</div>
          <pre className={styles.promptPreviewText}>{renderWithHighlights(content)}</pre>
        </div>
      </div>

      {/* 预览弹窗 */}
      <Modal
        title="提示词预览"
        visible={previewVisible}
        onCancel={() => setPreviewVisible(false)}
        footer={
          <Button onClick={() => setPreviewVisible(false)}>
            {t['close'] || '关闭'}
          </Button>
        }
        style={{ width: 700 }}
      >
        <Spin loading={previewLoading}>
          <pre className={styles.previewContent}>{previewText}</pre>
        </Spin>
      </Modal>

      {/* 版本历史弹窗 */}
      <PromptVersionHistory
        promptKey={promptKey}
        visible={historyVisible}
        onClose={() => setHistoryVisible(false)}
      />
    </Spin>
  );
}

export default PromptEditor;
