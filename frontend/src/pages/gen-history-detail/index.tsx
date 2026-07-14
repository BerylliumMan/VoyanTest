import React, { useState, useEffect, useMemo, useCallback } from 'react';
import { useParams, useHistory } from 'react-router-dom';
import {
  Card,
  Table,
  Typography,
  Button,
  Tag,
  Spin,
  Message,
  Space,
  Select,
  Modal,
  Form,
  Input,
  Collapse,
} from '@arco-design/web-react';
import { IconLeft, IconMenu, IconPlus, IconCopy, IconDelete, IconCode, IconDownload } from '@arco-design/web-react/icon';
import axios from 'axios';
import styles from './style/index.module.less';

const { Title, Text } = Typography;
const FormItem = Form.Item;

interface FunctionalPoint {
  id: number;
  module: string;
  name: string;
  category: string;
  description: string;
}

interface TestCase {
  test_case_id: string;
  module: string;
  title: string;
  preconditions: string;
  test_steps: string;
  expected_result: string;
  priority: string;
  selected: boolean;
}

interface GenPreviewResponse {
  session_id: string;
  functional_points: FunctionalPoint[];
  test_cases: TestCase[];
}

interface HistoryItem {
  id: string;
  filename: string;
  status: string;
  functional_points_count: number;
  test_cases_count: number;
}

interface ProjectItem {
  id: number;
  name: string;
}

interface Step {
  step_order: number;
  description: string;
  parsed_result: string;
}

const getStatusTag = (status: string) => {
  switch (status) {
    case 'completed':
      return <Tag color="green">完成</Tag>;
    case 'failed':
      return <Tag color="red">失败</Tag>;
    case 'analyzing':
      return <Tag color="blue">分析中</Tag>;
    default:
      return <Tag color="gray">{status}</Tag>;
  }
};

const getPriorityTag = (priority: string) => {
  switch (priority) {
    case '高':
      return <Tag color="red">{priority}</Tag>;
    case '中':
      return <Tag color="orange">{priority}</Tag>;
    case '低':
      return <Tag color="blue">{priority}</Tag>;
    default:
      return <Tag>{priority}</Tag>;
  }
};

const TruncateText: React.FC<{ text: string; maxWidth?: number }> = ({
  text,
  maxWidth = 200,
}) => (
  <span
    title={text}
    style={{
      display: 'inline-block',
      maxWidth,
      overflow: 'hidden',
      textOverflow: 'ellipsis',
      whiteSpace: 'nowrap',
    }}
  >
    {text || '-'}
  </span>
);

const splitNumberedItems = (text: string): string[] => {
  if (!text) return [];
  const parts = text.split(/\d+\.\s*/).filter((p) => p.trim());
  return parts.map((p) => p.trim());
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

const GenHistoryDetailPage: React.FC = () => {
  const { id } = useParams<{ id: string }>();
  const history = useHistory();

  const [previewData, setPreviewData] = useState<GenPreviewResponse | null>(null);
  const [historyItem, setHistoryItem] = useState<HistoryItem | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [testCasesPage, setTestCasesPage] = useState(1);
  const [testCasesPageSize, setTestCasesPageSize] = useState(20);
  const [selectedRowKeys, setSelectedRowKeys] = useState<string[]>([]);
  const [projects, setProjects] = useState<ProjectItem[]>([]);
  const [selectedProjectId, setSelectedProjectId] = useState<number | undefined>(undefined);
  const [importLoading, setImportLoading] = useState(false);
  const [editingTestCase, setEditingTestCase] = useState<TestCase | null>(null);
  const [editModalVisible, setEditModalVisible] = useState(false);
  const [editForm] = Form.useForm();
  const [editSubmitting, setEditSubmitting] = useState(false);
  const [editSteps, setEditSteps] = useState<Step[]>([]);
  const [copiedStep, setCopiedStep] = useState<Step | null>(null);

  useEffect(() => {
    const fetchData = async () => {
      setLoading(true);
      setError(null);
      try {
        const [previewRes, historyRes] = await Promise.all([
          axios.get<GenPreviewResponse>(`/api/gen/history/${id}`),
          axios.get<{ items: HistoryItem[] }>('/api/gen/history', {
            params: { page: 1, page_size: 1 },
          }),
        ]);

        setPreviewData(previewRes.data);

        const matchedItem = historyRes.data.items.find(
          (item) => item.id === id
        );
        if (matchedItem) {
          setHistoryItem(matchedItem);
        } else {
          setHistoryItem({
            id,
            filename: `会话 ${id.slice(0, 8)}...`,
            status: 'completed',
            functional_points_count: previewRes.data.functional_points.length,
            test_cases_count: previewRes.data.test_cases.length,
          });
        }
      } catch (err: unknown) {
        const axiosError = err as { response?: { status?: number; data?: { detail?: string } } };
        const detail = axiosError?.response?.data?.detail || '加载失败';
        if (axiosError?.response?.status === 410) {
          Message.warning('会话已过期，详细数据不可用。仅支持查看当前进程内的分析结果。');
        } else {
          Message.error(detail);
        }
        setError(detail);
      } finally {
        setLoading(false);
      }
    };

    fetchData();
  }, [id]);

  const refreshData = useCallback(async () => {
    try {
      const previewRes = await axios.get<GenPreviewResponse>(`/api/gen/history/${id}`);
      setPreviewData(previewRes.data);
      setSelectedRowKeys([]);
      if (historyItem) {
        setHistoryItem((prev) => prev ? {
          ...prev,
          functional_points_count: previewRes.data.functional_points.length,
          test_cases_count: previewRes.data.test_cases.length,
        } : prev);
      }
    } catch {
      Message.error('刷新数据失败');
    }
  }, [id, historyItem]);

  useEffect(() => {
    const fetchProjects = async () => {
      try {
        const res = await axios.get<ProjectItem[]>('/api/projects/');
        setProjects(res.data || []);
      } catch {
        return;
      }
    };
    fetchProjects();
  }, []);

  const handleImport = async (mode: 'all' | 'selected') => {
    if (!selectedProjectId) {
      Message.warning('请先选择项目');
      return;
    }
    if (mode === 'selected' && selectedRowKeys.length === 0) {
      Message.warning('请先选择要导入的测试用例');
      return;
    }
    setImportLoading(true);
    try {
      const res = await axios.post<{ imported_count: number; test_case_ids: number[] }>('/api/gen/import', {
        session_id: id,
        project_id: selectedProjectId,
        selected_ids: mode === 'selected' ? selectedRowKeys : null,
      });
      Message.success(`成功导入 ${res.data.imported_count} 条测试用例`);
      setSelectedRowKeys([]);
    } catch {
      Message.error('导入失败');
    } finally {
      setImportLoading(false);
    }
  };

  const parseNumberedText = (text: string): string[] => {
    if (!text) return [];
    const parts = text.split(/\d+\.\s*/).filter((p) => p.trim());
    return parts.map((p) => p.trim());
  };

  const handleEdit = (record: TestCase) => {
    setEditingTestCase(record);
    const stepTexts = parseNumberedText(record.test_steps || '');
    const resultTexts = parseNumberedText(record.expected_result || '');
    const steps: Step[] = stepTexts.map((desc, idx) => ({
      step_order: idx + 1,
      description: desc,
      parsed_result: resultTexts[idx] || '',
    }));
    if (steps.length === 0) {
      steps.push({ step_order: 1, description: '', parsed_result: '' });
    }
    setEditSteps(steps);
    setCopiedStep(null);
    editForm.setFieldsValue({
      module: record.module,
      title: record.title,
      preconditions: record.preconditions,
      priority: record.priority,
    });
    setEditModalVisible(true);
  };

  const handleEditSubmit = async () => {
    if (!editingTestCase) return;
    try {
      const values = await editForm.validate();
      setEditSubmitting(true);
      const testSteps = editSteps.map((s, i) => `${i + 1}. ${s.description}`).join('\n');
      const expectedResult = editSteps.map((s, i) => `${i + 1}. ${s.parsed_result}`).join('\n');
      await axios.put(`/api/gen/history/${id}/test-cases/${editingTestCase.test_case_id}`, {
        ...values,
        test_steps: testSteps,
        expected_result: expectedResult,
      });
      Message.success('更新成功');
      setEditModalVisible(false);
      setEditingTestCase(null);
      refreshData();
    } catch {
      return;
    } finally {
      setEditSubmitting(false);
    }
  };

  const addStep = () => setEditSteps([...editSteps, { step_order: editSteps.length + 1, description: '', parsed_result: '' }]);
  const removeStep = (idx: number) => setEditSteps(editSteps.filter((_, i) => i !== idx).map((s, i) => ({ ...s, step_order: i + 1 })));
  const updateStep = (idx: number, field: string, value: string) => {
    const newSteps = [...editSteps];
    newSteps[idx] = { ...newSteps[idx], [field]: value };
    setEditSteps(newSteps);
  };

  const handleDragStart = (idx: number) => (e: React.DragEvent) => {
    e.stopPropagation();
    e.dataTransfer.effectAllowed = 'move';
    e.dataTransfer.setData('text/plain', String(idx));
  };
  const handleDragOver = (idx: number) => (e: React.DragEvent) => {
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';
    e.currentTarget.classList.add('drag-over');
  };
  const handleDragLeave = (idx: number) => (e: React.DragEvent) => {
    e.currentTarget.classList.remove('drag-over');
  };
  const handleDrop = (targetIdx: number) => (e: React.DragEvent) => {
    e.preventDefault();
    e.currentTarget.classList.remove('drag-over');
    const sourceIdx = parseInt(e.dataTransfer.getData('text/plain'));
    if (isNaN(sourceIdx) || sourceIdx === targetIdx) return;
    const newSteps = [...editSteps];
    const [moved] = newSteps.splice(sourceIdx, 1);
    newSteps.splice(targetIdx, 0, moved);
    setEditSteps(newSteps.map((s, i) => ({ ...s, step_order: i + 1 })));
  };

  const insertStep = (idx: number) => {
    const newSteps = [...editSteps];
    newSteps.splice(idx, 0, { step_order: idx + 1, description: '', parsed_result: '' });
    setEditSteps(newSteps.map((s, i) => ({ ...s, step_order: i + 1 })));
  };
  const copyStep = (idx: number) => {
    setCopiedStep(editSteps[idx]);
    Message.success('已复制步骤');
  };
  const pasteStep = (idx: number) => {
    if (!copiedStep) return;
    const newSteps = [...editSteps];
    newSteps.splice(idx + 1, 0, { ...copiedStep, step_order: idx + 2 });
    setEditSteps(newSteps.map((s, i) => ({ ...s, step_order: i + 1 })));
  };

  const handleDelete = async (record: TestCase) => {
    try {
      await axios.delete(`/api/gen/history/${id}/test-cases/${record.test_case_id}`);
      Message.success('删除成功');
      refreshData();
    } catch {
      Message.error('删除失败');
    }
  };

  const paginatedTestCases = useMemo(() => {
    if (!previewData) return [];
    const start = (testCasesPage - 1) * testCasesPageSize;
    return previewData.test_cases.slice(start, start + testCasesPageSize);
  }, [previewData, testCasesPage, testCasesPageSize]);

  const functionalPointColumns = [
    {
      title: '序号',
      dataIndex: 'id',
      width: 80,
    },
    {
      title: '模块',
      dataIndex: 'module',
      width: 120,
    },
    {
      title: '名称',
      dataIndex: 'name',
      width: 200,
    },
    {
      title: '分类',
      dataIndex: 'category',
      width: 120,
    },
    {
      title: '描述',
      dataIndex: 'description',
    },
  ];

  const testCaseColumns = [
    {
      title: '序号',
      width: 80,
      render: (_: unknown, __: TestCase, index: number) =>
        (testCasesPage - 1) * testCasesPageSize + index + 1,
    },
    {
      title: '用例ID',
      dataIndex: 'test_case_id',
      width: 120,
      render: (val: string) => (
        <Text copyable={{ text: val }} className={styles.copyableText}>
          {val.length > 12 ? `${val.slice(0, 12)}...` : val}
        </Text>
      ),
    },
    {
      title: '模块',
      dataIndex: 'module',
      width: 120,
    },
    {
      title: '标题',
      dataIndex: 'title',
      width: 200,
      render: (val: string) => <TruncateText text={val} maxWidth={180} />,
    },
    {
      title: '前置条件',
      dataIndex: 'preconditions',
      width: 180,
      render: (val: string) => <TruncateText text={val} maxWidth={160} />,
    },
    {
      title: '测试步骤',
      dataIndex: 'test_steps',
      width: 300,
      render: (val: string) => <NumberedList text={val} />,
    },
    {
      title: '预期结果',
      dataIndex: 'expected_result',
      width: 300,
      render: (val: string) => <NumberedList text={val} />,
    },
    {
      title: '优先级',
      dataIndex: 'priority',
      width: 100,
      render: (val: string) => getPriorityTag(val),
    },
    {
      title: '操作',
      width: 120,
      render: (_: unknown, record: TestCase) => (
        <Space size={8}>
          <Button type="text" size="small" onClick={() => handleEdit(record)}>
            编辑
          </Button>
          <Button
            type="text"
            size="small"
            status="danger"
            onClick={() => {
              Modal.confirm({
                title: '确认删除',
                content: `确定要删除测试用例 "${record.title}" 吗？此操作不可恢复。`,
                onOk: () => handleDelete(record),
              });
            }}
          >
            删除
          </Button>
        </Space>
      ),
    },
  ];

  if (loading) {
    return (
      <div className={styles.container}>
        <div className={styles.loadingContainer}>
          <Spin size={40} tip="加载中…" />
        </div>
      </div>
    );
  }

  if (error) {
    return (
      <div className={styles.container}>
        <Card>
          <div className={styles.header}>
            <div className={styles.headerLeft}>
              <Button
                type="text"
                icon={<IconLeft />}
                onClick={() => history.goBack()}
              >
                返回
              </Button>
              <Title heading={5}>分析详情</Title>
            </div>
          </div>
          <div className={styles.errorState}>
            <Text type="secondary">{error}</Text>
          </div>
        </Card>
      </div>
    );
  }

  return (
    <div className={styles.container}>
      <Card>
        <div className={styles.header}>
          <div className={styles.headerLeft}>
            <Button
              type="text"
              icon={<IconLeft />}
              onClick={() => history.goBack()}
            >
              返回
            </Button>
            <Title heading={5} className={styles.titleNoMargin}>
              {historyItem?.filename || '分析详情'}
            </Title>
            {historyItem && getStatusTag(historyItem.status)}
          </div>
        </div>

        {historyItem && (
          <div className={styles.toolbar}>
            <div className={styles.stats}>
              <Space size={24}>
                <Text>
                  功能点: <Text bold>{historyItem.functional_points_count}</Text>
                </Text>
                <Text>
                  测试用例: <Text bold>{historyItem.test_cases_count}</Text>
                </Text>
              </Space>
            </div>
            <div className={styles.importActions}>
              <Select
                placeholder="选择目标项目"
                className={styles.projectSelect}
                value={selectedProjectId}
                onChange={(val) => setSelectedProjectId(val as number)}
                allowClear
              >
                {projects && projects.map((p) => (
                  <Select.Option key={p.id} value={p.id}>
                    {p.name}
                  </Select.Option>
                ))}
              </Select>
              <Button
                type="primary"
                disabled={!selectedProjectId}
                loading={importLoading}
                onClick={() => handleImport('all')}
              >
                全部导入
              </Button>
              <Button
                type="primary"
                status="success"
                disabled={!selectedProjectId || selectedRowKeys.length === 0}
                loading={importLoading}
                onClick={() => handleImport('selected')}
              >
                导入选中
              </Button>
              <Button
                icon={<IconDownload />}
                onClick={() => {
                  const a = document.createElement('a');
                  a.href = `/api/gen/history/${id}/export-xlsx`;
                  a.download = `测试用例_${id.slice(0, 8)}.xlsx`;
                  a.click();
                }}
              >
                导出 xlsx
              </Button>
            </div>
          </div>
        )}

        {previewData && (
          <>
            <div className={styles.section}>
              <Title heading={6}>功能点 ({previewData.functional_points.length})</Title>
              <Collapse defaultActiveKey={[]}>
                {previewData.functional_points.map((fp) => (
                  <Collapse.Item
                    key={String(fp.id)}
                    name={String(fp.id)}
                    header={
                      <span>
                        <Tag color="arcoblue" size="small" className={styles.tagMarginRight}>
                          {fp.module || '通用'}
                        </Tag>
                        <Tag size="small" className={styles.tagMarginRight}>
                          {fp.category || ''}
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

            <div className={styles.section}>
              <Title heading={6}>测试用例 ({previewData.test_cases.length})</Title>
              <Table
                rowKey="test_case_id"
                columns={testCaseColumns}
                data={paginatedTestCases}
                rowSelection={{
                  type: 'checkbox',
                  selectedRowKeys,
                  onChange: (keys: (string | number)[]) => setSelectedRowKeys(keys.map(String)),
                }}
                pagination={{
                  current: testCasesPage,
                  pageSize: testCasesPageSize,
                  total: previewData.test_cases.length,
                  onChange: setTestCasesPage,
                  onPageSizeChange: setTestCasesPageSize,
                  showTotal: true,
                  sizeCanChange: true,
                }}
                size="small"
                border={false}
              />
            </div>
          </>
        )}
      </Card>

      <Modal
        title="编辑测试用例"
        visible={editModalVisible}
        onCancel={() => {
          setEditModalVisible(false);
          setEditingTestCase(null);
        }}
        onOk={handleEditSubmit}
        confirmLoading={editSubmitting}
        unmountOnExit
        className={styles.modalWide}
      >
        <Form form={editForm} layout="vertical">
          <FormItem label="模块" field="module" rules={[{ required: true, message: '请输入模块名称' }]}>
            <Input placeholder="请输入模块" />
          </FormItem>
          <FormItem label="标题" field="title" rules={[{ required: true, message: '请输入标题' }]}>
            <Input placeholder="请输入标题" />
          </FormItem>
          <FormItem label="前置条件" field="preconditions">
            <Input.TextArea placeholder="请输入前置条件" autoSize={{ minRows: 2, maxRows: 4 }} />
          </FormItem>
          <FormItem label="测试步骤与预期结果">
            <div>
              {editSteps.map((step, idx) => (
                <div key={idx} className={`step-row ${styles['step-row']}`}
                  onDragOver={handleDragOver(idx)}
                  onDragLeave={handleDragLeave(idx)}
                  onDrop={handleDrop(idx)}
                >
                  <Button type="text" icon={<IconMenu />}
                    draggable
                    onDragStart={handleDragStart(idx)}
                    className={styles['drag-handle']}
                  />
                  <Tag className={styles['step-number-tag']}>{idx + 1}</Tag>
                  <Input.TextArea
                    className={styles['step-input']}
                    placeholder="测试步骤"
                    value={step.description}
                    onChange={(v) => updateStep(idx, 'description', v)}
                    autoSize={{ minRows: 1, maxRows: 6 }}
                  />
                  <Input.TextArea
                    className={styles['step-input']}
                    placeholder="预期结果"
                    value={step.parsed_result || ''}
                    onChange={(v) => updateStep(idx, 'parsed_result', v)}
                    autoSize={{ minRows: 1, maxRows: 6 }}
                  />
                  <Button type="text" icon={<IconPlus />} onClick={() => insertStep(idx)} title="在上方插入" />
                  <Button type="text" icon={<IconCopy />} onClick={() => copyStep(idx)} title="复制" />
                  {copiedStep && (
                    <Button type="text" icon={<IconPlus />} onClick={() => pasteStep(idx)} title="粘贴" />
                  )}
                  <Button type="text" status="danger" icon={<IconDelete />} onClick={() => removeStep(idx)} />
                </div>
              ))}
              <Button type="dashed" long onClick={addStep}>添加步骤</Button>
            </div>
          </FormItem>
          <FormItem label="优先级" field="priority" rules={[{ required: true, message: '请选择优先级' }]}>
            <Select placeholder="请选择优先级">
              <Select.Option value="高">高</Select.Option>
              <Select.Option value="中">中</Select.Option>
              <Select.Option value="低">低</Select.Option>
            </Select>
          </FormItem>
        </Form>
      </Modal>
    </div>
  );
};

export default GenHistoryDetailPage;
