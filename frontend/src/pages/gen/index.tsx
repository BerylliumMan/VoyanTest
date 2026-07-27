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
import logger from '@/utils/logger';
import styles from './style/index.module.less';

const { Title, Text } = Typography;

// Module-level cache to survive Arco Space remounting


const splitNumberedItems = (text: string): string[] => {
  if (!text) return [];
  const src = text.trim();
  const clean = (items: string[]) =>
    items.map((p) => p.replace(/\s+/g, ' ').trim()).filter(Boolean);
  if (src.includes('\n')) {
    const lineRe = /(?:^|\n)\s*\d+[\.、]\s+([\s\S]+?)(?=\n\s*\d+[\.、]\s+|$)/g;
    const lineItems: string[] = [];
    let m: RegExpExecArray | null;
    while ((m = lineRe.exec(src)) !== null) lineItems.push(m[1]);
    if (lineItems.length >= 2) return clean(lineItems);
  }
  const inlineRe = /(?:^|\s)\d+[\.、]\s+([\s\S]+?)(?=\s+\d+[\.、]\s+|$)/g;
  const inlineItems: string[] = [];
  let m2: RegExpExecArray | null;
  while ((m2 = inlineRe.exec(src)) !== null) inlineItems.push(m2[1]);
  if (inlineItems.length) return clean(inlineItems);
  return src.split('\n').map((p) => p.trim()).filter(Boolean);
};

const NumberedList: React.FC<{ text: string }> = ({ text }) => {
  const items = splitNumberedItems(text);
  if (items.length === 0) return <span>-</span>;
  return (
    <ol className={styles.numberedList}>
      {items.map((item, idx) => (
        <li key={idx} className={styles.numberedListItem}>{item}</li>
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

interface GenAgent {
  id: number;
  name: string;
  description: string;
  skills: string[];
  is_active: boolean;
}

const agentModeLabel = (agent: GenAgent): string => {
  if ((agent.skills || []).includes('tc_generate_ui')) return 'UI自动化用例';
  return '功能用例';
};

const GenPage: React.FC = () => {
  const t = useLocale();
  const [projects, setProjects] = useState<Project[]>([]);
  const [selectedProject, setSelectedProject] = useState<number | undefined>(undefined);
  const [genAgents, setGenAgents] = useState<GenAgent[]>([]);
  const [selectedAgentId, setSelectedAgentId] = useState<number | undefined>(undefined);
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

  useEffect(() => {
    axios
      .get('/api/projects/')
      .then((res) => setProjects(res.data || []))
      .catch((err) => Message.error(err?.response?.data?.detail || 'Failed to load projects'));
    axios
      .get('/api/gen/agents')
      .then((res) => {
        const agents: GenAgent[] = res.data || [];
        setGenAgents(agents);
        const active = agents.find((a) => a.is_active) || agents[0];
        if (active) setSelectedAgentId(active.id);
      })
      .catch(() => {
        /* optional — generation still works with server default */
      });
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
    if (selectedAgentId != null) {
      formData.append('agent_id', String(selectedAgentId));
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
      setAnalysisStatus({
        status: 'analyzing',
        progress: 5,
        message: '正在解析文档',
      });
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
        const raw = res.data || {};
        const status: AnalysisStatus = {
          status: raw.status,
          progress: typeof raw.progress === 'number' ? raw.progress : 0,
          message: raw.message || raw.error_message || '',
          functional_points: raw.functional_points,
          test_cases: raw.test_cases,
        };
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
        logger.error('Polling error:', err);
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
        <Space direction="vertical" size="large" className={styles.fullWidth}>
          <div>
            <Title heading={5}>项目信息</Title>
            <Space className={styles.fullWidth} direction="vertical">
              <div className={styles.flexCenter}>
                <Text className={styles.formLabel}>选择项目：</Text>
                <Select
                  className={styles.selectNarrow}
                  placeholder="请选择项目"
                  value={selectedProject}
                  onChange={(v) =>
                    setSelectedProject(typeof v === 'number' ? v : undefined)
                  }
                  options={projects.map((p) => ({ label: p.name, value: p.id }))}
                />
              </div>
              {genAgents.length > 0 && (
                <div className={styles.flexCenter}>
                  <Text className={styles.formLabel}>生成 Agent：</Text>
                  <Select
                    className={styles.selectNarrow}
                    placeholder="选择用例生成助手"
                    value={selectedAgentId}
                    onChange={(v) =>
                      setSelectedAgentId(typeof v === 'number' ? v : undefined)
                    }
                    options={genAgents.map((a) => ({
                      label: `${a.name}（${agentModeLabel(a)}）`,
                      value: a.id,
                    }))}
                  />
                </div>
              )}
              <div className={styles.flexRow}>
                <Text className={styles.formLabelWithTop}>项目描述（可选）：</Text>
                <Input.TextArea
                  className={styles.inputMaxWidth}
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
                  <Space direction="vertical" className={styles.fullWidth} size="large">
                    <div>
                      <div className={styles.stepRow}>
                        {[
                          { label: '文档解析', icon: <IconFile aria-hidden /> },
                          { label: '测试项提取', icon: <IconCode aria-hidden /> },
                          { label: '生成用例', icon: <IconStar aria-hidden /> },
                        ].map((s, i) => {
                          const msg = analysisStatus.message || '';
                          const step =
                            msg.includes('用例') ? 2
                            : msg.includes('测试项') || msg.includes('功能点') || msg.includes('提取') ? 1
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
                        {['文档解析', '测试项提取', '生成用例'].map((label, i) => {
                          const msg = analysisStatus.message || '';
                          const step =
                            msg.includes('用例') ? 2
                            : msg.includes('测试项') || msg.includes('功能点') || msg.includes('提取') ? 1
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
                    <div className={styles.analyzingCentered}>
                      <Space>
                        <IconLoading spin className={styles.primaryIcon} />
                        <Text>{analysisStatus.message || '分析中…'}</Text>
                      </Space>
                    </div>
                  </Space>
                </Card>
              )}

              {analysisStatus.status === 'completed' && (
                <Card>
                  <div className={styles.resultCentered}>
                    <IconCheck className={styles.successIconLarge} aria-hidden />
                    <div className={styles.resultTitle}>
                      <Text className={styles.successTitle}>
                        分析完成
                      </Text>
                    </div>
                    <Text className={styles.mutedText}>
                      共提取 {functionalPoints.length} 个测试项，生成 {testCases.length} 个测试用例
                    </Text>
                  </div>
                </Card>
              )}

              {analysisStatus.status === 'failed' && (
                <Card>
                  <div className={styles.resultCentered}>
                    <IconClose className={styles.dangerIconLarge} aria-hidden />
                    <div className={styles.resultTitle}>
                      <Text className={styles.dangerText}>
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
                <Title heading={5}>测试项 ({functionalPoints.length})</Title>
                <Collapse defaultActiveKey={[]}>
                  {functionalPoints.map((fp, i) => (
                    <Collapse.Item
                      key={String(fp.id)}
                      name={String(fp.id)}
                      header={
                        <span>
                          <Tag color="arcoblue" size="small" className={styles.tagMarginRight}>
                            {fp.module || '通用'}
                          </Tag>
                          {fp.name}
                        </span>
                      }
                    >
                      <div className={styles.description}>
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


        </Space>
      </Card>
    </div>
  );
};

export default GenPage;