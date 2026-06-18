import React, { useState, useRef, useEffect, useMemo } from 'react';
import {
  Card, Upload, Button, Message, Typography, Space, Select,
  Table, Tag, Progress, Divider, Collapse, Input, Spin, List,
} from '@arco-design/web-react';
import type { UploadItem } from '@arco-design/web-react/es/Upload/interface';
import {
  IconUpload, IconCheck, IconClose, IconLoading,
  IconThunderbolt, IconSave, IconHistory, IconFile, IconCode,
  IconStar,
} from '@arco-design/web-react/icon';
import axios from 'axios';
import useLocale from '@/utils/useLocale';
import styles from './style/index.module.less';

const { Title, Text } = Typography;

// Module-level cache to survive Arco Space remounting
let cachedPrompts: PromptItem[] | null = null;

const splitNumberedItems = (text: string): string[] => {
  if (!text) return [];
  const parts = text.split(/\d+\.\s*/).filter((p) => p.trim());
  return parts.map((p) => p.trim());
};

const NumberedList: React.FC<{ text: string }> = ({ text }) => {
  const items = splitNumberedItems(text);
  if (items.length === 0) return <span>-</span>;
  return (
    <ol style={{ margin: 0, paddingLeft: 20 }}>
      {items.map((item, idx) => (
        <li key={idx} style={{ marginBottom: 4 }}>{item}</li>
      ))}
    </ol>
  );
};

interface Project {
  id: number;
  name: string;
}

interface FunctionalPoint {
  id: string;
  module: string;
  name: string;
  description: string;
  priority: string;
}

interface TestCase {
  test_case_id: string;
  title: string;
  module: string;
  priority: string;
  preconditions: string;
  test_steps: string;
  expected_result: string;
  selected: boolean;
}

interface AnalysisStatus {
  status: 'pending' | 'analyzing' | 'completed' | 'failed';
  progress: number;
  message: string;
  functional_points?: FunctionalPoint[];
  test_cases?: TestCase[];
}

interface PromptItem {
  template_key: string;
  label: string;
  template_content: string;
  is_custom: boolean;
  default_content: string;
  updated_at?: string;
}

const GenPage: React.FC = () => {
  const t = useLocale();
  const [projects, setProjects] = useState<Project[]>([]);
  const [selectedProject, setSelectedProject] = useState<number | undefined>(undefined);
  const [description, setDescription] = useState('');
  const [fileList, setFileList] = useState<UploadItem[]>([]);
  const [uploading, setUploading] = useState(false);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [analysisStatus, setAnalysisStatus] = useState<AnalysisStatus | null>(null);
  const [functionalPoints, setFunctionalPoints] = useState<FunctionalPoint[]>([]);
  const [testCases, setTestCases] = useState<TestCase[]>([]);
  const [selectedRowKeys, setSelectedRowKeys] = useState<string[]>([]);
  const [importing, setImporting] = useState(false);
  const pollTimer = useRef<ReturnType<typeof setInterval> | null>(null);

  // Prompt config state (init from module-level cache)
  const [prompts, setPrompts] = useState<PromptItem[]>(cachedPrompts || []);
  const [selectedPrompt, setSelectedPrompt] = useState<PromptItem | null>(null);
  const [editedContent, setEditedContent] = useState('');
  const [promptSaving, setPromptSaving] = useState(false);
  const [promptLoading, setPromptLoading] = useState(false);

  useEffect(() => {
    axios
      .get('/api/projects/')
      .then((res) => setProjects(res.data || []))
      .catch((err) => Message.error(err?.response?.data?.detail || 'Failed to load projects'));
  }, []);

  useEffect(() => {
    return () => {
      if (pollTimer.current) {
        clearInterval(pollTimer.current);
      }
    };
  }, []);

  const handleUpload = async () => {
    if (!selectedProject) {
      Message.warning('请先选择项目');
      return;
    }
    if (fileList.length === 0) {
      Message.warning('请上传文件');
      return;
    }

    setUploading(true);
    setSessionId(null);
    setAnalysisStatus(null);
    setFunctionalPoints([]);
    setTestCases([]);
    setSelectedRowKeys([]);

    const formData = new FormData();
    formData.append('project_id', String(selectedProject));
    if (description) {
      formData.append('project_description', description);
    }
    fileList.forEach((file) => {
      if (file.originFile) {
        formData.append('files', file.originFile);
      }
    });

    try {
      const res = await axios.post('/api/gen/upload', formData, {
        headers: { 'Content-Type': 'multipart/form-data' },
        timeout: 60000,
      });
      setSessionId(res.data.session_id);
      Message.success('上传成功，开始分析');
      startPolling(res.data.session_id);
    } catch (e: unknown) {
      const err = e as { code?: string; response?: { data?: { detail?: string } } };
      if (err.code === 'ECONNABORTED') {
        Message.error('上传超时，请检查网络连接或减小文件大小');
      } else {
        Message.error(err?.response?.data?.detail || '上传失败');
      }
      setUploading(false);
    }
  };

  const startPolling = (sid: string) => {
    pollTimer.current = setInterval(async () => {
      try {
        const res = await axios.get(`/api/gen/status/${sid}`);
        const status: AnalysisStatus = res.data;
        setAnalysisStatus(status);

        if (status.status === 'completed') {
          if (pollTimer.current) {
            clearInterval(pollTimer.current);
            pollTimer.current = null;
          }
          setUploading(false);
          await loadPreview(sid);
        } else if (status.status === 'failed') {
          if (pollTimer.current) {
            clearInterval(pollTimer.current);
            pollTimer.current = null;
          }
          setUploading(false);
          Message.error(status.message || '分析失败');
        }
      } catch (err) {
        console.error('Polling error:', err);
      }
    }, 2000);
  };

  const loadPreview = async (sid: string) => {
    try {
      const res = await axios.get(`/api/gen/preview/${sid}`);
      setFunctionalPoints(res.data.functional_points || []);
      setTestCases(res.data.test_cases || []);
    } catch (e: unknown) {
      const err = e as { response?: { data?: { detail?: string } } };
      Message.error(err?.response?.data?.detail || '加载预览失败');
    }
  };

  const handleImport = async (allIds?: string[]) => {
    if (!sessionId || !selectedProject) {
      Message.warning('请先选择项目');
      return;
    }
    const idsToImport = allIds || selectedRowKeys;
    if (idsToImport.length === 0) {
      Message.warning('请选择要导入的用例');
      return;
    }

    setImporting(true);
    try {
      await axios.post('/api/gen/import', {
        session_id: sessionId,
        project_id: selectedProject,
        selected_ids: idsToImport,
      });
      Message.success(`成功导入 ${idsToImport.length} 个用例`);
      setSelectedRowKeys([]);
      // 导入成功后保留列表，不清空数据
    } catch (e: unknown) {
      const err = e as { response?: { data?: { detail?: string } } };
      Message.error(err?.response?.data?.detail || '导入失败');
    } finally {
      setImporting(false);
    }
  };

  // Prompt config handlers
  const loadPrompts = async () => {
    if (cachedPrompts) {
      setPrompts(cachedPrompts);
      return;
    }
    setPromptLoading(true);
    try {
      const res = await axios.get('/api/config/prompts');
      const list: PromptItem[] = res.data;
      setPrompts(list);
      cachedPrompts = list;
      if (list.length > 0 && !selectedPrompt) {
        setSelectedPrompt(list[0]);
        setEditedContent(list[0].template_content);
      }
    } catch (e: unknown) {
      const err = e as { response?: { data?: { detail?: string } } };
      Message.error(err.response?.data?.detail || '加载提示词失败');
    } finally {
      setPromptLoading(false);
    }
  };

  const handlePromptSelect = (item: PromptItem) => {
    setSelectedPrompt(item);
    setEditedContent(item.template_content);
  };

  const handlePromptSave = async () => {
    if (!selectedPrompt) return;
    setPromptSaving(true);
    try {
      await axios.put(`/api/config/prompts/${selectedPrompt.template_key}`, {
        template_content: editedContent,
      });
      Message.success('保存成功');
      loadPrompts();
    } catch (e: unknown) {
      const err = e as { response?: { data?: { detail?: string } } };
      Message.error(err.response?.data?.detail || '保存失败');
    } finally {
      setPromptSaving(false);
    }
  };

  const handlePromptRestore = async () => {
    if (!selectedPrompt) return;
    setPromptSaving(true);
    try {
      const res = await axios.post(`/api/config/prompts/${selectedPrompt.template_key}/restore`);
      const restored: PromptItem = res.data;
      setSelectedPrompt(restored);
      setEditedContent(restored.template_content);
      Message.success('已恢复默认');
      loadPrompts();
    } catch (e: unknown) {
      const err = e as { response?: { data?: { detail?: string } } };
      Message.error(err.response?.data?.detail || '恢复失败');
    } finally {
      setPromptSaving(false);
    }
  };

  const hasPromptChanges = selectedPrompt && editedContent !== selectedPrompt.template_content;

  const columns = [
    { title: '用例ID', dataIndex: 'test_case_id', width: 100 },
    { title: '标题', dataIndex: 'title', width: 300 },
    {
      title: '模块',
      dataIndex: 'module',
      width: 150,
    },
    {
      title: '优先级',
      dataIndex: 'priority',
      width: 100,
      render: (value: string) => {
        const color = value === '高' ? 'red' : value === '中' ? 'orange' : 'green';
        return <Tag color={color}>{value}</Tag>;
      },
    },
    {
      title: '测试步骤',
      dataIndex: 'test_steps',
      width: 300,
      render: (value: string) => <NumberedList text={value} />,
    },
    {
      title: '预期结果',
      dataIndex: 'expected_result',
      width: 300,
      render: (value: string) => <NumberedList text={value} />,
    },
  ];

  return (
    <div className={styles.container}>
      <Card>
        <Space direction="vertical" size="large" style={{ width: '100%' }}>
          <div>
            <Title heading={5}>项目信息</Title>
            <Space style={{ width: '100%' }} direction="vertical">
              <div style={{ display: 'flex', alignItems: 'center' }}>
                <Text style={{ width: 120, flexShrink: 0 }}>选择项目：</Text>
                <Select
                  style={{ width: 300 }}
                  placeholder="请选择项目"
                  value={selectedProject}
                  onChange={(v) =>
                    setSelectedProject(typeof v === 'number' ? v : undefined)
                  }
                  options={projects.map((p) => ({ label: p.name, value: p.id }))}
                />
              </div>
              <div style={{ display: 'flex' }}>
                <Text style={{ width: 120, flexShrink: 0, paddingTop: 4 }}>项目描述（可选）：</Text>
                <Input.TextArea
                  style={{ maxWidth: 600 }}
                  placeholder="描述项目的功能和测试重点…"
                  value={description}
                  onChange={(e) => setDescription(e)}
                  rows={3}
                />
              </div>
            </Space>
          </div>

          <Divider />

          <div>
            <Title heading={5}>上传需求文档</Title>
            <Upload
              drag
              accept=".docx,.pdf,.md,.png,.jpg,.jpeg"
              fileList={fileList}
              onChange={setFileList}
              autoUpload={false}
              multiple
              tip="支持 .docx, .pdf, .md, .png, .jpg, .jpeg 格式"
            />
          </div>

          <Divider />

          <div>
            <Space>
              <Button
                type="primary"
                icon={<IconThunderbolt />}
                loading={uploading}
                onClick={handleUpload}
                disabled={!selectedProject || fileList.length === 0}
              >
                开始分析
              </Button>
              {testCases.length > 0 && (
                <>
                  <Button
                    type="primary"
                    status="success"
                    icon={<IconCheck />}
                    loading={importing}
                    onClick={() => handleImport()}
                    disabled={selectedRowKeys.length === 0}
                  >
                    导入选中用例 ({selectedRowKeys.length})
                  </Button>
                  <Button
                    type="primary"
                    icon={<IconCheck />}
                    loading={importing}
                    onClick={() => handleImport(testCases.map(tc => tc.test_case_id))}
                  >
                    全部导入 ({testCases.length})
                  </Button>
                </>
              )}
            </Space>
          </div>

          {analysisStatus && (
            <>
              <Divider />
              {analysisStatus.status === 'analyzing' && (
                <Card className={styles.analyzingCard}>
                  <Space direction="vertical" style={{ width: '100%' }} size="large">
                    <div>
                      <div className={styles.stepRow}>
                        {[
                          { label: '文档解析', icon: <IconFile aria-hidden /> },
                          { label: '功能点提取', icon: <IconCode aria-hidden /> },
                          { label: '生成用例', icon: <IconStar aria-hidden /> },
                        ].map((s, i) => {
                          const msg = analysisStatus.message || '';
                          const step =
                            msg.includes('用例') ? 2
                            : msg.includes('功能点') || msg.includes('提取') ? 1
                            : 0;
                          const isActive = i === step;
                          const isDone = i < step;
                          return (
                            <React.Fragment key={s.label}>
                              {i > 0 && (
                                <div
                                  className={`${styles.stepLine} ${
                                    isDone ? styles.stepLineActive : styles.stepLinePending
                                  }`}
                                />
                              )}
                              <div
                                className={`${styles.stepDot} ${
                                  isDone ? styles.stepDotDone
                                  : isActive ? styles.stepDotActive
                                  : styles.stepDotPending
                                }`}
                              >
                                {isDone ? <IconCheck aria-hidden /> : isActive ? <IconLoading spin aria-hidden /> : i + 1}
                              </div>
                            </React.Fragment>
                          );
                        })}
                      </div>
                      <div className={styles.stepLabels}>
                        {['文档解析', '功能点提取', '生成用例'].map((label, i) => {
                          const msg = analysisStatus.message || '';
                          const step =
                            msg.includes('用例') ? 2
                            : msg.includes('功能点') || msg.includes('提取') ? 1
                            : 0;
                          const isActive = i === step;
                          const isDone = i < step;
                          return (
                            <div
                              key={label}
                              className={`${styles.stepLabel} ${
                                isDone ? styles.stepLabelDone
                                : isActive ? styles.stepLabelActive
                                : ''
                              }`}
                            >
                              {label}
                            </div>
                          );
                        })}
                      </div>
                    </div>

                    <div className={styles.analyzingProgress}>
                      <Progress
                        percent={analysisStatus.progress}
                        animation
                        formatText={() => ''}
                      />
                    </div>

                    {/* 状态信息 */}
                    <div style={{ textAlign: 'center' }}>
                      <Space>
                        <IconLoading spin style={{ color: 'rgb(var(--primary-6))' }} />
                        <Text>{analysisStatus.message || '分析中…'}</Text>
                      </Space>
                    </div>
                  </Space>
                </Card>
              )}

              {analysisStatus.status === 'completed' && (
                <Card>
                  <div style={{ textAlign: 'center', padding: '12px 0' }}>
                    <IconCheck style={{ fontSize: 48, color: 'rgb(var(--success-6))' }} aria-hidden />
                    <div style={{ marginTop: 8 }}>
                      <Text style={{ fontSize: 16, fontWeight: 600, color: 'rgb(var(--success-6))' }}>
                        分析完成
                      </Text>
                    </div>
                    <Text style={{ color: 'var(--color-text-3)' }}>
                      共提取 {functionalPoints.length} 个功能点，生成 {testCases.length} 个测试用例
                    </Text>
                  </div>
                </Card>
              )}

              {analysisStatus.status === 'failed' && (
                <Card>
                  <div style={{ textAlign: 'center', padding: '12px 0' }}>
                    <IconClose style={{ fontSize: 48, color: 'rgb(var(--danger-6))' }} aria-hidden />
                    <div style={{ marginTop: 8 }}>
                      <Text style={{ color: 'rgb(var(--danger-6))' }}>
                        {analysisStatus.message || '分析失败'}
                      </Text>
                    </div>
                  </div>
                </Card>
              )}
            </>
          )}

          {functionalPoints.length > 0 && (
            <>
              <Divider />
              <div className={styles.resultSection}>
                <Title heading={5}>功能点 ({functionalPoints.length})</Title>
                <Collapse defaultActiveKey={[]}>
                  {functionalPoints.map((fp, i) => (
                    <Collapse.Item
                      key={String(fp.id)}
                      name={String(fp.id)}
                      header={
                        <span>
                          <Tag color="arcoblue" size="small" style={{ marginRight: 8 }}>
                            {fp.module || '通用'}
                          </Tag>
                          {fp.name}
                        </span>
                      }
                    >
                      <div style={{ whiteSpace: 'pre-wrap', color: 'var(--color-text-2)', fontSize: 13, lineHeight: 1.7 }}>
                        {fp.description || '暂无描述'}
                      </div>
                    </Collapse.Item>
                  ))}
                </Collapse>
              </div>
            </>
          )}

          {testCases.length > 0 && (
            <>
              <Divider />
              <div className={styles.tcTable}>
                <Title heading={5}>测试用例 ({testCases.length})</Title>
                <Table
                  rowKey="test_case_id"
                  columns={columns}
                  data={testCases}
                  rowSelection={{
                    type: 'checkbox',
                    selectedRowKeys,
                    preserveSelectedRowKeys: true,
                    onChange: (keys) => setSelectedRowKeys(keys.map(String)),
                  }}
                  pagination={{ pageSize: 10 }}
                />
              </div>
            </>
          )}

          <Divider />

          <Collapse
            defaultActiveKey={[]}
            onChange={(keys) => {
              if (keys.includes('prompt-config') && !cachedPrompts) loadPrompts();
            }}
          >
            <Collapse.Item header="提示词配置" name="prompt-config">
              <Space style={{ width: '100%' }} align="start">
                <div style={{ width: 240, flexShrink: 0 }}>
                  {promptLoading ? (
                    <Spin style={{ display: 'block', margin: '20px auto' }} />
                  ) : prompts.length === 0 ? (
                    <Text style={{ color: 'var(--color-text-3)' }}>暂无模板</Text>
                  ) : (
                    <List
                      size="small"
                      bordered
                      dataSource={prompts}
                      render={(item) => (
                        <List.Item
                          key={item.template_key}
                          onClick={() => handlePromptSelect(item)}
                          style={{
                            cursor: 'pointer',
                            background: selectedPrompt?.template_key === item.template_key
                              ? 'var(--color-primary-light-1)' : undefined,
                          }}
                        >
                          <List.Item.Meta
                            title={
                              <Space>
                                <Text bold={item.is_custom}>{item.label}</Text>
                                {item.is_custom && <Tag color="blue" size="small">已修改</Tag>}
                              </Space>
                            }
                            description={<Text style={{ fontSize: 12 }}>{item.template_key}</Text>}
                          />
                        </List.Item>
                      )}
                    />
                  )}
                </div>
                <div style={{ flex: 1, minWidth: 0 }}>
                  {selectedPrompt ? (
                    <>
                      <Space style={{ marginBottom: 12 }}>
                        <Text style={{ fontWeight: 600, fontSize: 14 }}>{selectedPrompt.label}</Text>
                        {selectedPrompt.is_custom && (
                          <Tag color="blue" size="small">已自定义</Tag>
                        )}
                      </Space>
                      <Input.TextArea
                        style={{ minHeight: 300, fontFamily: 'monospace', fontSize: 13 }}
                        value={editedContent}
                        onChange={setEditedContent}
                      />
                      <Space style={{ marginTop: 12 }}>
                        <Button
                          type="primary"
                          icon={<IconSave />}
                          loading={promptSaving}
                          disabled={!hasPromptChanges}
                          onClick={handlePromptSave}
                        >
                          保存
                        </Button>
                        <Button
                          icon={<IconHistory />}
                          loading={promptSaving}
                          disabled={!selectedPrompt.is_custom}
                          onClick={handlePromptRestore}
                        >
                          恢复默认
                        </Button>
                      </Space>
                    </>
                  ) : (
                    <div style={{ textAlign: 'center', padding: 60, color: 'var(--color-text-3)' }}>
                      请从左侧选择一个提示词模板
                    </div>
                  )}
                </div>
              </Space>
            </Collapse.Item>
          </Collapse>
        </Space>
      </Card>
    </div>
  );
};

export default GenPage;