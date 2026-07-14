import React, { useState, useEffect } from 'react';
import { Card, Table, Button, Message, Tag, Space, Modal, Typography, Select } from '@arco-design/web-react';
import { IconDelete, IconEye, IconRefresh, IconImport, IconDownload } from '@arco-design/web-react/icon';
import { useHistory } from 'react-router-dom';
import axios from 'axios';
import styles from './style/index.module.less';

const { Title, Text } = Typography;

interface GenHistoryItem {
  id: string;
  filename: string;
  filenames: string[];
  project_id: number | null;
  project_name: string;
  project_description: string;
  status: string;
  error_message: string;
  functional_points_count: number;
  test_cases_count: number;
  imported_count: number;
  created_at: string;
  completed_at: string | null;
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
  const [importing, setImporting] = useState<string | null>(null);

  const fetchData = async () => {
    setLoading(true);
    try {
      const params: Record<string, string | number> = { page, page_size: pageSize };
      if (selectedProject) {
        params.project_id = selectedProject;
      }
      const res = await axios.get('/api/gen/history', { params });
      setData(res.data.items || []);
      setTotal(res.data.total || 0);
    } catch (e: unknown) {
      const err = e as { response?: { data?: { detail?: string } } };
      Message.error(err?.response?.data?.detail || '加载失败');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchData();
  }, [page, pageSize, selectedProject]);

  useEffect(() => {
    axios
      .get('/api/projects/')
      .then((res) => setProjects(res.data || []))
      .catch(() => {});
  }, []);

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

  const handleImport = async (sessionId: string) => {
    if (!selectedProject) {
      Message.warning('请先选择目标项目');
      return;
    }

    setImporting(sessionId);
    try {
      const res = await axios.post('/api/gen/import', {
        session_id: sessionId,
        project_id: selectedProject,
      });
      Message.success(`成功导入 ${res.data.imported_count} 个用例`);
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
      title: '状态',
      dataIndex: 'status',
      width: 100,
      render: (val: string) => getStatusTag(val),
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
        <span
          className={val > 0 ? styles.importedCount : undefined}
        >
          {val} / {record.test_cases_count}
        </span>
      ),
    },
    {
      title: '操作',
      dataIndex: 'actions',
      width: 200,
      render: (_: unknown, record: GenHistoryItem) => (
        <Space>
          <Button
            type="text"
            size="small"
            icon={<IconEye />}
            onClick={() => handleViewDetail(record.id)}
            disabled={record.status !== 'completed'}
            aria-label="查看"
          />
          <Button
            type="text"
            size="small"
            icon={<IconImport />}
            loading={importing === record.id}
            onClick={() => handleImport(record.id)}
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
              placeholder="全部项目"
              allowClear
              value={selectedProject}
              onChange={(v) =>
                setSelectedProject(typeof v === 'number' ? v : undefined)
              }
              options={[
                ...projects.map((p) => ({ label: p.name, value: p.id })),
              ]}
            />
            <Button icon={<IconRefresh />} onClick={fetchData} loading={loading}>
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
    </div>
  );
};

export default GenHistoryPage;
