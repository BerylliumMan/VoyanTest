import React, { useCallback, useEffect, useState } from 'react';
import {
  Button,
  Empty,
  Modal,
  Popconfirm,
  Space,
  Spin,
  Table,
  Tag,
  Typography,
  Upload,
} from '@arco-design/web-react';
import { IconDelete, IconUpload } from '@arco-design/web-react/icon';
import { apiDelete, apiGet, apiPost } from '@/utils/apiRequest';
import { TestFile } from '../types';

interface TestFilePickerProps {
  visible: boolean;
  /** 归属项目（上传与列表过滤；缺省为全局文件） */
  projectId?: number | null;
  onCancel: () => void;
  /** 选中文件 → 父组件写入 platform://<id> */
  onSelect: (file: TestFile) => void;
}

const humanSize = (n: number): string => {
  if (!n && n !== 0) return '—';
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
};

/**
 * 平台测试文件选择/管理（031 US1 T018）。
 *
 * 契约（contracts §1.7）：上传 POST /api/api-test/files（multipart/form-data，≤ api_test_file_max_mb）；
 * 被用例引用的文件删除返回 409，需 `force=true` 二次确认；本组件把选中项交给父组件
 * 写成 `platform://<id>`（执行端由服务端解析为路径，见 core/api_runner/runner.py）。
 */
const TestFilePicker: React.FC<TestFilePickerProps> = ({
  visible,
  projectId,
  onCancel,
  onSelect,
}) => {
  const [files, setFiles] = useState<TestFile[]>([]);
  const [loading, setLoading] = useState(false);
  const [uploading, setUploading] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const data = await apiGet<{ total: number; items: TestFile[] }>(
        '/api/api-test/files',
        projectId ? { project_id: projectId } : undefined
      );
      setFiles(Array.isArray(data) ? data : data?.items ?? []);
    } catch {
      setFiles([]);
    } finally {
      setLoading(false);
    }
  }, [projectId]);

  useEffect(() => {
    if (visible) load();
  }, [visible, load]);

  const handleUpload = async (file: File) => {
    setUploading(true);
    try {
      const form = new FormData();
      form.append('file', file);
      if (projectId) form.append('project_id', String(projectId));
      await apiPost('/api/api-test/files', form, '文件已上传');
      await load();
    } catch {
      /* apiRequest 已弹错误（超限为 413 可读提示） */
    } finally {
      setUploading(false);
    }
    return 'ok';
  };

  const handleDelete = async (row: TestFile, force = false) => {
    try {
      await apiDelete(
        `/api/api-test/files/${row.id}${force ? '?force=true' : ''}`,
        '文件已删除'
      );
      await load();
    } catch {
      /* 已弹错误 */
    }
  };

  return (
    <Modal
      title="测试文件"
      visible={visible}
      onCancel={onCancel}
      footer={null}
      style={{ width: 760 }}
      unmountOnExit
    >
      <Space style={{ marginBottom: 12 }}>
        <Upload
          showUploadList={false}
          customRequest={({ file }) => handleUpload(file as File)}
        >
          <Button type="primary" icon={<IconUpload />} loading={uploading}>
            上传文件
          </Button>
        </Upload>
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          单文件上限见系统配置；被用例引用的文件删除需二次确认
        </Typography.Text>
      </Space>

      <Spin loading={loading} style={{ width: '100%' }}>
        {files.length === 0 && !loading ? (
          <Empty description="暂无测试文件，先上传一个" />
        ) : (
          <Table
            rowKey="id"
            data={files}
            size="small"
            pagination={false}
            scroll={{ y: 320 }}
            columns={[
              { title: '文件名', dataIndex: 'name' },
              {
                title: '大小',
                dataIndex: 'size',
                width: 100,
                render: (v: number) => humanSize(v),
              },
              {
                title: '引用',
                dataIndex: 'ref_count',
                width: 110,
                render: (v: number) =>
                  v > 0 ? <Tag color="arcoblue">{v} 条用例</Tag> : <Tag>未引用</Tag>,
              },
              {
                title: '操作',
                width: 130,
                render: (_: unknown, row: TestFile) => (
                  <Space>
                    <Button type="text" size="small" onClick={() => onSelect(row)}>
                      选择
                    </Button>
                    {row.ref_count > 0 ? (
                      <Popconfirm
                        title={`该文件被 ${row.ref_count} 条用例引用，删除后这些用例会执行失败。确认强制删除？`}
                        okText="强制删除"
                        onOk={() => handleDelete(row, true)}
                      >
                        <Button type="text" size="small" status="danger" icon={<IconDelete />} />
                      </Popconfirm>
                    ) : (
                      <Popconfirm title="确认删除该文件？" onOk={() => handleDelete(row)}>
                        <Button type="text" size="small" status="danger" icon={<IconDelete />} />
                      </Popconfirm>
                    )}
                  </Space>
                ),
              },
            ]}
          />
        )}
      </Spin>
    </Modal>
  );
};

export default TestFilePicker;
