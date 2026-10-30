import React, { useEffect, useState } from 'react';
import { Button, Empty, Message, Popconfirm, Select, Space, Table, Tag } from '@arco-design/web-react';
import { IconDelete, IconRefresh } from '@arco-design/web-react/icon';
import { apiRequest } from '@/utils/apiRequest';

interface ProjectOption {
  id: number;
  name: string;
}

interface MemoryRow {
  name: string;
  description: string;
  type: string;
  written_at: string;
  body_preview: string;
}

const TYPE_LABEL: Record<string, string> = {
  user: '用户画像',
  feedback: '工作偏好',
  project: '项目约定',
  reference: '外部资源',
};

/**
 * 044 US4：项目记忆管理。
 * 记忆由助手在对话中自动写入，或用户显式要求写入；这里提供查看与删除。
 */
function MemoryPanel() {
  const [projects, setProjects] = useState<ProjectOption[]>([]);
  const [projectId, setProjectId] = useState<number | undefined>();
  const [rows, setRows] = useState<MemoryRow[]>([]);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    apiRequest<ProjectOption[] | { items: ProjectOption[] }>({
      method: 'get',
      url: '/api/projects',
    })
      .then((data) => {
        const list = Array.isArray(data) ? data : (data as { items?: ProjectOption[] }).items || [];
        setProjects(list);
        if (list.length > 0) setProjectId(list[0].id);
      })
      .catch(() => setProjects([]));
  }, []);

  const fetchRows = async (pid?: number) => {
    const target = pid ?? projectId;
    if (!target) {
      setRows([]);
      return;
    }
    setLoading(true);
    try {
      const data = await apiRequest<{ items: MemoryRow[] }>({
        method: 'get',
        url: `/api/chat/memory?project_id=${target}`,
      });
      setRows(data?.items || []);
    } catch (e: any) {
      Message.error(e?.response?.data?.detail || '加载记忆失败');
      setRows([]);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    if (projectId) fetchRows(projectId);
  }, [projectId]);

  const handleDelete = async (name: string) => {
    if (!projectId) return;
    try {
      await apiRequest({
        method: 'delete',
        url: `/api/chat/memory/${encodeURIComponent(name)}?project_id=${projectId}`,
      });
      Message.success('已删除');
      fetchRows();
    } catch (e: any) {
      Message.error(e?.response?.data?.detail || '删除失败');
    }
  };

  return (
    <Space direction="vertical" style={{ width: '100%' }} size="medium">
      <Space>
        <span>项目：</span>
        <Select
          style={{ width: 260 }}
          placeholder="选择项目"
          value={projectId}
          onChange={(v) => setProjectId(v as number)}
          options={projects.map((p) => ({ label: p.name, value: p.id }))}
        />
        <Button icon={<IconRefresh />} onClick={() => fetchRows()}>刷新</Button>
      </Space>
      <Table<MemoryRow>
        rowKey="name"
        loading={loading}
        data={rows}
        pagination={{ pageSize: 20 }}
        noDataElement={<Empty description="该项目还没有记忆" />}
        columns={[
          { title: '名称', dataIndex: 'name', width: 180 },
          { title: '描述', dataIndex: 'description' },
          {
            title: '类型',
            dataIndex: 'type',
            width: 120,
            render: (v: string) => <Tag color="arcoblue">{TYPE_LABEL[v] || v}</Tag>,
          },
          { title: '写入时间', dataIndex: 'written_at', width: 190 },
          {
            title: '操作',
            width: 90,
            render: (_: unknown, record: MemoryRow) => (
              <Popconfirm title="删除后不再召回，确认？" onOk={() => handleDelete(record.name)}>
                <Button size="mini" status="danger" icon={<IconDelete />}>删除</Button>
              </Popconfirm>
            ),
          },
        ]}
      />
    </Space>
  );
}

export default MemoryPanel;
