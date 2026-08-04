import React, { useState, useEffect, useRef } from 'react';
import {
  Card,
  Table,
  Button,
  Message,
  Tag,
  Space,
  Modal,
  Typography,
  Select,
  Progress,
  Tooltip,
} from '@arco-design/web-react';
import {
  IconDelete,
  IconEye,
  IconRefresh,
  IconImport,
  IconDownload,
  IconPause,
  IconSync,
} from '@arco-design/web-react/icon';
import { useHistory } from 'react-router-dom';
import axios from 'axios';
import styles from './style/index.module.less';

const { Title } = Typography;

interface GenHistoryItem {
  id: string;
  filename: string;
  filenames: string[];
  project_id: number | null;
  project_name: string;
  project_description: string;
  status: string;
  error_message: string;
  progress: number;
  progress_message: string;
  functional_points_count: number;
  test_cases_count: number;
  imported_count: number;
  case_kind?: 'ui' | 'functional' | string;
  created_at: string;
  completed_at: string | null;
  can_retry?: boolean;
}

interface Project {
  id: number;
  name: string;
}

const GenHistoryPage: React.FC = () => {
  const history = useHistory();
  const [data, setData] = useState<GenHistoryItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);
  const [projects, setProjects] = useState<Project[]>([]);
  const [selectedProject, setSelectedProject] = useState<number | undefined>(undefined);
  const [filterKind, setFilterKind] = useState<string | undefined>(undefined);
  const [importing, setImporting] = useState<string | null>(null);
  const [stopping, setStopping] = useState<string | null>(null);
  const [retrying, setRetrying] = useState<string | null>(null);
  const [importModalVisible, setImportModalVisible] = useState(false);
  const [importTarget, setImportTarget] = useState<GenHistoryItem | null>(null);
  const [importProjectId, setImportProjectId] = useState<number | undefined>(undefined);
  const [importCaseKind, setImportCaseKind] = useState<'ui' | 'functional'>('ui');
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const dataRef = useRef<GenHistoryItem[]>([]);
  dataRef.current = data;

  const fetchData = async (silent = false) => {
    if (!silent) {
      setLoading(true);
    }
    try {
      const params: Record<string, string | number> = { page, page_size: pageSize };
      if (selectedProject) {
        params.project_id = selectedProject;
      }
      if (filterKind) {
        params.case_kind = filterKind;
      }
      const res = await axios.get('/api/gen/history', { params });
      setData(res.data.items || []);
      setTotal(res.data.total || 0);
    } catch (e: unknown) {
      const err = e as { response?: { data?: { detail?: string } } };
      Message.error(err?.response?.data?.detail || '加载失败');
    } finally {
      if (!silent) {
        setLoading(false);
      }
    }
  };

  useEffect(() => {
    fetchData();
  }, [page, pageSize, selectedProject, filterKind]);

  useEffect(() => {
    pollRef.current = setInterval(() => {
      const needsPoll = dataRef.current.some(
        (row) => row.status === 'analyzing' || row.status === 'pending',
      );
      if (needsPoll) {
        fetchData(true);
      }
    }, 3000);
    return () => {
      if (pollRef.current) {
        clearInterval(pollRef.current);
        pollRef.current = null;
      }
    };
  }, [page, pageSize, selectedProject, filterKind]);

  useEffect(() => {
    axios
      .get('/api/projects/')
      .then((res) => setProjects(res.data || []))
      .catch(() => {});
  }, []);

  const handleStop = (id: string) => {
    Modal.confirm({
      title: '停止分析',
      content: '确定要停止该分析任务吗？已生成的中间结果不会保留。',
      onOk: async () => {
        setStopping(id);
        try {
          await axios.post(`/api/gen/history/${id}/cancel`);
          Message.success('已停止分析');
          fetchData();
        } catch (e: unknown) {
          const err = e as { response?: { data?: { detail?: string } } };
          Message.error(err?.response?.data?.detail || '停止失败');
        } finally {
          setStopping(null);
        }
      },
    });
  };

  const handleRetry = (record: GenHistoryItem) => {
    Modal.confirm({
      title: '重新分析',
      content: '将使用原上传文件重新分析，并覆盖本次会话已有结果。确定继续？',
      onOk: async () => {
        setRetrying(record.id);
        try {
          await axios.post(`/api/gen/history/${record.id}/retry`);
          Message.success('已开始重新分析');
          fetchData();
        } catch (e: unknown) {
          const err = e as { response?: { data?: { detail?: string } } };
          Message.error(err?.response?.data?.detail || '重试失败');
        } finally {
          setRetrying(null);
        }
      },
    });
  };

  const handleDelete = (id: string) => {
    Modal.confirm({
      title: '确认删除',
      content: '确定要删除这条分析记录吗？',
      onOk: async () => {
        try {
          await axios.delete(`/api/gen/history/${id}`);
          Message.success('删除成功');
          fetchData();
        } catch (e: unknown) {
          const err = e as { response?: { data?: { detail?: string } } };
          Message.error(err?.response?.data?.detail || '删除失败');
        }
      },
    });
  };

  const handleImportClick = (record: GenHistoryItem) => {
    const kind = record.case_kind === 'functional' ? 'functional' : 'ui';
    setImportTarget(record);
    setImportCaseKind(kind);
    setImportProjectId(
      typeof record.project_id === 'number'
        ? record.project_id
        : selectedProject,
    );
    setImportModalVisible(true);
  };

  const handleImportConfirm = async () => {
    if (!importTarget) return;
    if (!importProjectId) {
      Message.warning('请选择导入目标项目');
      return;
    }
    const projectName =
      projects.find((p) => p.id === importProjectId)?.name || String(importProjectId);
    const kindLabel = importCaseKind === 'ui' ? 'UI 用例' : '功能用例';
    setImporting(importTarget.id);
    try {
      const res = await axios.post('/api/gen/import', {
        session_id: importTarget.id,
        project_id: importProjectId,
        case_kind: importCaseKind,
      });
      const skipped = res.data.skipped_count || 0;
      Message.success(
        skipped > 0
          ? `已导入 ${res.data.imported_count} 条${kindLabel}到「${projectName}」（跳过重复 ${skipped}）`
          : `已导入 ${res.data.imported_count} 条${kindLabel}到「${projectName}」`,
      );
      setImportModalVisible(false);
      setImportTarget(null);
      fetchData();
    } catch (e: unknown) {
      const err = e as { response?: { data?: { detail?: string }; status?: number } };
      const detail = err?.response?.data?.detail || '导入失败';
      if (err?.response?.status === 404) {
        Message.error('会话已过期，无法导入。请重新进行分析。');
      } else {
        Message.error(detail);
      }
    } finally {
      setImporting(null);
    }
  };

  const handleViewDetail = (id: string) => {
    history.push(`/gen-history-detail/${id}`);
  };

  const getCaseKindTag = (kind?: string) =>
    kind === 'functional' ? (
      <Tag color="arcoblue">功能</Tag>
    ) : (
      <Tag color="purple">UI</Tag>
    );

  const getStatusTag = (status: string) => {
    switch (status) {
      case 'completed':
        return <Tag color="green">完成</Tag>;
      case 'failed':
        return <Tag color="red">失败</Tag>;
      case 'analyzing':
        return <Tag color="blue">分析中</Tag>;
      case 'cancelled':
        return <Tag color="orange">已停止</Tag>;
      default:
        return <Tag color="gray">{status}</Tag>;
    }
  };

  const renderProgress = (record: GenHistoryItem) => {
    const pct = Math.max(0, Math.min(100, Number(record.progress) || 0));
    const running = record.status === 'analyzing' || record.status === 'pending';
    let status: 'success' | 'error' | 'normal' | undefined = 'normal';
    if (record.status === 'completed') status = 'success';
    else if (record.status === 'failed') status = 'error';
    else if (record.status === 'cancelled') status = 'normal';

    const tip =
      record.progress_message ||
      record.error_message ||
      (running ? '分析中…' : record.status === 'completed' ? '分析完成' : '');

    return (
      <Tooltip content={tip || undefined}>
        <div className={styles.progressCell}>
          <Progress
            percent={running && pct <= 0 ? 5 : pct}
            size="small"
            status={running ? undefined : status}
            animation={running}
            showText
          />
          {tip ? <div className={styles.progressMsg}>{tip}</div> : null}
        </div>
      </Tooltip>
    );
  };

  const columns = [
    {
      title: '分析时间',
      dataIndex: 'created_at',
      width: 180,
      render: (val: string) => new Date(val).toLocaleString('zh-CN'),
    },
    {
      title: '文件名',
      dataIndex: 'filename',
      width: 200,
      render: (val: string, record: GenHistoryItem) => (
        <span title={record.filenames.join(', ')}>
          {record.filenames.length > 1 ? `${val} 等${record.filenames.length}个文件` : val}
        </span>
      ),
    },
    {
      title: '项目',
      dataIndex: 'project_name',
      width: 140,
      render: (val: string) => val || <span className={styles.placeholderText}>-</span>,
    },
    {
      title: '类型',
      dataIndex: 'case_kind',
      width: 90,
      render: (val: string) => getCaseKindTag(val),
    },
    {
      title: '状态',
      dataIndex: 'status',
      width: 100,
      render: (val: string) => getStatusTag(val),
    },
    {
      title: '进度',
      dataIndex: 'progress',
      width: 200,
      render: (_: unknown, record: GenHistoryItem) => renderProgress(record),
    },
    {
      title: '功能点',
      dataIndex: 'functional_points_count',
      width: 80,
    },
    {
      title: '用例数',
      dataIndex: 'test_cases_count',
      width: 80,
    },
    {
      title: '已导入',
      dataIndex: 'imported_count',
      width: 80,
      render: (val: number, record: GenHistoryItem) => (
        <span className={val > 0 ? styles.importedCount : undefined}>
          {val} / {record.test_cases_count}
        </span>
      ),
    },
    {
      title: '操作',
      dataIndex: 'actions',
      width: 280,
      render: (_: unknown, record: GenHistoryItem) => (
        <Space>
          {(record.status === 'analyzing' || record.status === 'pending') && (
            <Button
              type="text"
              size="small"
              status="warning"
              icon={<IconPause />}
              loading={stopping === record.id}
              onClick={() => handleStop(record.id)}
              aria-label="停止分析"
            />
          )}
          {record.can_retry && (
            <Button
              type="text"
              size="small"
              icon={<IconSync />}
              loading={retrying === record.id}
              onClick={() => handleRetry(record)}
              aria-label="重试"
            >
              重试
            </Button>
          )}
          <Button
            type="text"
            size="small"
            icon={<IconEye />}
            onClick={() => handleViewDetail(record.id)}
            disabled={record.status === 'analyzing' || record.status === 'pending'}
            aria-label="查看"
          />
          <Button
            type="text"
            size="small"
            icon={<IconImport />}
            loading={importing === record.id}
            onClick={() => handleImportClick(record)}
            disabled={record.status !== 'completed'}
            aria-label="导入"
          />
          <Button
            type="text"
            size="small"
            icon={<IconDownload />}
            disabled={record.status !== 'completed'}
            onClick={() => {
              const a = document.createElement('a');
              a.href = `/api/gen/history/${record.id}/export-xlsx`;
              a.download = `测试用例_${record.id.slice(0, 8)}.xlsx`;
              a.click();
            }}
            aria-label="导出"
          />
          <Button
            type="text"
            size="small"
            icon={<IconDelete />}
            status="danger"
            disabled={record.status === 'analyzing' || record.status === 'pending'}
            onClick={() => handleDelete(record.id)}
            aria-label="删除"
          />
        </Space>
      ),
    },
  ];

  return (
    <div className={styles.container}>
      <Card>
        <div className={styles.header}>
          <Title heading={5}>分析记录</Title>
          <Space>
            <Select
              className={styles.projectSelect}
              placeholder="筛选项目"
              allowClear
              value={selectedProject}
              onChange={(v) =>
                setSelectedProject(typeof v === 'number' ? v : undefined)
              }
              options={[
                ...projects.map((p) => ({ label: p.name, value: p.id })),
              ]}
            />
            <Select
              style={{ width: 120 }}
              placeholder="用例类型"
              allowClear
              value={filterKind}
              onChange={(v) => setFilterKind(v || undefined)}
              options={[
                { label: 'UI', value: 'ui' },
                { label: '功能', value: 'functional' },
              ]}
            />
            <Button icon={<IconRefresh />} onClick={() => fetchData()} loading={loading}>
              刷新
            </Button>
          </Space>
        </div>
        <Table
          rowKey="id"
          columns={columns}
          data={data}
          loading={loading}
          pagination={{
            current: page,
            pageSize,
            total,
            onChange: setPage,
            onPageSizeChange: setPageSize,
            showTotal: true,
            sizeCanChange: true,
          }}
        />
      </Card>
      <Modal
        title="导入到测试用例"
        visible={importModalVisible}
        onOk={handleImportConfirm}
        onCancel={() => {
          setImportModalVisible(false);
          setImportTarget(null);
        }}
        confirmLoading={!!importing}
        okText="确认导入"
      >
        {importTarget && (
          <Space direction="vertical" style={{ width: '100%' }} size={12}>
            <Typography.Text type="secondary">
              文件：{importTarget.filename}（共 {importTarget.test_cases_count} 条）
            </Typography.Text>
            <div>
              <div style={{ marginBottom: 6 }}>目标项目</div>
              <Select
                style={{ width: '100%' }}
                placeholder="选择要导入到的项目"
                value={importProjectId}
                onChange={(v) => setImportProjectId(v as number)}
                options={projects.map((p) => ({ label: p.name, value: p.id }))}
              />
            </div>
            <div>
              <div style={{ marginBottom: 6 }}>用例类型（导入后归入对应页签）</div>
              <Select
                style={{ width: '100%' }}
                value={importCaseKind}
                onChange={(v) => setImportCaseKind(v as 'ui' | 'functional')}
                options={[
                  { label: 'UI 用例', value: 'ui' },
                  { label: '功能用例', value: 'functional' },
                ]}
              />
            </div>
            <Typography.Text type="secondary">
              模块路径按用例内「一级——二级」自动创建；导入后可在「测试用例」页对应类型下查看。
            </Typography.Text>
          </Space>
        )}
      </Modal>
    </div>
  );
};

export default GenHistoryPage;
