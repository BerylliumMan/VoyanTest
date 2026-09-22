import React, { useCallback, useEffect, useRef, useState } from 'react';
import {
  Alert,
  Button,
  Drawer,
  Empty,
  Input,
  Message,
  Popconfirm,
  Space,
  Spin,
  Table,
  Typography,
  Upload,
} from '@arco-design/web-react';
import type { UploadItem } from '@arco-design/web-react/es/Upload/interface';
import type { ColumnProps } from '@arco-design/web-react/es/Table/interface';
import { IconDelete, IconEdit, IconPlus, IconUpload } from '@arco-design/web-react/icon';
import { apiRequest } from '@/utils/apiRequest';
import {
  DATASET_IMPORT_MAX_BYTES,
  DATASET_ROW_LIMIT,
  Dataset,
  DatasetImportResult,
  DatasetPayload,
} from '../types';
import styles from '../style/index.module.less';

const { Text } = Typography;

interface EditableRow {
  id: number;
  cells: string[];
}

interface DatasetDraft {
  id: number | null;
  name: string;
  columns: string[];
  rows: EditableRow[];
}

interface DatasetPanelProps {
  visible: boolean;
  onClose: () => void;
  projectId: number | null;
  onChanged?: () => void;
}

/** 后端 rows 可能是 {列名:值} 对象，也可能是与 columns 对齐的数组；统一转成字符串单元格 */
const toCellArray = (row: unknown, columns: string[]): string[] => {
  if (Array.isArray(row)) {
    return columns.map((_, i) => String((row as unknown[])[i] ?? ''));
  }
  if (row && typeof row === 'object') {
    const obj = row as Record<string, unknown>;
    return columns.map((c) => String(obj[c] ?? ''));
  }
  return columns.map(() => '');
};

const readColumns = (ds: Dataset): string[] =>
  Array.isArray(ds.columns) ? ds.columns.map(String) : [];

const readRawRows = (ds: Dataset): unknown[] =>
  Array.isArray(ds.rows) ? ds.rows : [];

const formatTime = (value?: string): string => {
  if (!value) return '-';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? '-' : date.toLocaleString();
};

const DatasetPanel: React.FC<DatasetPanelProps> = ({
  visible,
  onClose,
  projectId,
  onChanged,
}) => {
  const [datasets, setDatasets] = useState<Dataset[]>([]);
  const [loading, setLoading] = useState(false);
  const [listError, setListError] = useState<string | null>(null);
  const [draft, setDraft] = useState<DatasetDraft | null>(null);
  const [saving, setSaving] = useState(false);
  const [importing, setImporting] = useState(false);
  const [importError, setImportError] = useState<string | null>(null);
  const [csvFileList, setCsvFileList] = useState<UploadItem[]>([]);

  const rowIdRef = useRef(0);
  const nextRowId = useCallback(() => {
    rowIdRef.current += 1;
    return rowIdRef.current;
  }, []);

  const loadDatasets = useCallback(async () => {
    if (!projectId) {
      setDatasets([]);
      return;
    }
    setLoading(true);
    setListError(null);
    try {
      const data = await apiRequest<Dataset[]>(
        { method: 'GET', url: '/api/api-test/datasets', params: { project_id: projectId } },
        { showError: false }
      );
      // 契约：列表端点是分页形状 {total, items}（与项目其他列表一致），不是裸数组
      const items = Array.isArray(data) ? data : ((data as unknown as { items?: Dataset[] })?.items ?? []);
      setDatasets(items);
    } catch (error) {
      setDatasets([]);
      setListError(error instanceof Error ? error.message : '加载数据集失败');
    } finally {
      setLoading(false);
    }
  }, [projectId]);

  useEffect(() => {
    if (visible) {
      setDraft(null);
      setImportError(null);
      setCsvFileList([]);
      loadDatasets();
    }
  }, [visible, loadDatasets]);

  const startEdit = (ds: Dataset) => {
    const columns = readColumns(ds);
    setImportError(null);
    setDraft({
      id: ds.id,
      name: ds.name,
      columns,
      rows: readRawRows(ds).map((row) => ({
        id: nextRowId(),
        cells: toCellArray(row, columns),
      })),
    });
  };

  const startCreate = () => {
    setImportError(null);
    setDraft({ id: null, name: '', columns: [''], rows: [{ id: nextRowId(), cells: [''] }] });
  };

  const updateDraft = (patch: Partial<DatasetDraft>) =>
    setDraft((prev) => (prev ? { ...prev, ...patch } : prev));

  const updateColumn = (index: number, value: string) =>
    setDraft((prev) =>
      prev ? { ...prev, columns: prev.columns.map((c, i) => (i === index ? value : c)) } : prev
    );

  const addColumn = () =>
    setDraft((prev) =>
      prev
        ? {
            ...prev,
            columns: [...prev.columns, `列${prev.columns.length + 1}`],
            rows: prev.rows.map((row) => ({ ...row, cells: [...row.cells, ''] })),
          }
        : prev
    );

  const removeColumn = (index: number) =>
    setDraft((prev) =>
      prev
        ? {
            ...prev,
            columns: prev.columns.filter((_, i) => i !== index),
            rows: prev.rows.map((row) => ({
              ...row,
              cells: row.cells.filter((_, i) => i !== index),
            })),
          }
        : prev
    );

  const addRow = () =>
    setDraft((prev) =>
      prev
        ? {
            ...prev,
            rows: [...prev.rows, { id: nextRowId(), cells: prev.columns.map(() => '') }],
          }
        : prev
    );

  const removeRow = (rowId: number) =>
    setDraft((prev) => (prev ? { ...prev, rows: prev.rows.filter((r) => r.id !== rowId) } : prev));

  const updateCell = (rowId: number, cellIndex: number, value: string) =>
    setDraft((prev) =>
      prev
        ? {
            ...prev,
            rows: prev.rows.map((row) =>
              row.id === rowId
                ? { ...row, cells: row.cells.map((c, i) => (i === cellIndex ? value : c)) }
                : row
            ),
          }
        : prev
    );

  const validate = (value: DatasetDraft): string | null => {
    if (!value.name.trim()) return '请输入数据集名称';
    const columns = value.columns.map((c) => c.trim());
    if (columns.length === 0) return '请至少添加一列';
    if (columns.some((c) => !c)) return '列名不能为空';
    if (new Set(columns).size !== columns.length) return '列名不能重复';
    if (value.rows.length > DATASET_ROW_LIMIT) return `行数不能超过 ${DATASET_ROW_LIMIT} 行`;
    return null;
  };

  const handleSave = async () => {
    if (!draft) return;
    if (!projectId) {
      Message.warning('请先选择项目');
      return;
    }
    const invalid = validate(draft);
    if (invalid) {
      Message.warning(invalid);
      return;
    }
    const columns = draft.columns.map((c) => c.trim());
    const rows = draft.rows.map((row) =>
      Object.fromEntries(columns.map((column, i) => [column, row.cells[i] ?? '']))
    );
    const payload: DatasetPayload = { name: draft.name.trim(), columns, rows };
    setSaving(true);
    try {
      if (draft.id != null) {
        await apiRequest(
          { method: 'PUT', url: `/api/api-test/datasets/${draft.id}`, data: payload },
          { successMessage: '数据集已更新' }
        );
      } else {
        await apiRequest(
          { method: 'POST', url: '/api/api-test/datasets', data: { ...payload, project_id: projectId } },
          { successMessage: '数据集已创建' }
        );
      }
      setDraft(null);
      await loadDatasets();
      onChanged?.();
    } catch {
      /* apiRequest 已弹错误 */
    } finally {
      setSaving(false);
    }
  };

  const handleDelete = async (id: number) => {
    try {
      await apiRequest(
        { method: 'DELETE', url: `/api/api-test/datasets/${id}` },
        { successMessage: '数据集已删除' }
      );
      await loadDatasets();
      onChanged?.();
    } catch {
      /* apiRequest 已弹错误 */
    }
  };

  const handleCsvChange = async (fileList: UploadItem[]) => {
    const file = fileList[fileList.length - 1]?.originFile;
    setCsvFileList([]);
    if (!file) return;
    setImportError(null);
    if (file.size > DATASET_IMPORT_MAX_BYTES) {
      const message = '文件不能超过 2MB';
      setImportError(message);
      Message.error(message);
      return;
    }
    setImporting(true);
    try {
      const formData = new FormData();
      formData.append('file', file);
      const data = await apiRequest<DatasetImportResult>(
        {
          method: 'POST',
          url: '/api/api-test/datasets/import',
          data: formData,
          headers: { 'Content-Type': 'multipart/form-data' },
        },
        { showError: false }
      );
      const columns = Array.isArray(data?.columns) ? data.columns.map(String) : [];
      const rawRows = Array.isArray(data?.rows) ? data.rows : [];
      if (columns.length === 0) {
        const message = 'CSV 解析结果为空：请确认首行是表头且至少有一列';
        setImportError(message);
        Message.error(message);
        return;
      }
      if (rawRows.length > DATASET_ROW_LIMIT) {
        const message = `CSV 行数超过上限（${rawRows.length} > ${DATASET_ROW_LIMIT}）`;
        setImportError(message);
        Message.error(message);
        return;
      }
      setDraft({
        id: null,
        name: file.name.replace(/\.[^.]+$/, '') || '导入数据集',
        columns,
        rows: rawRows.map((row) => ({ id: nextRowId(), cells: toCellArray(row, columns) })),
      });
    } catch (error) {
      setImportError(error instanceof Error ? error.message : '导入失败');
    } finally {
      setImporting(false);
    }
  };

  const listColumns: ColumnProps<Dataset>[] = [
    { title: '名称', dataIndex: 'name', ellipsis: true },
    { title: '列数', width: 80, render: (_: unknown, record: Dataset) => readColumns(record).length },
    { title: '行数', width: 80, render: (_: unknown, record: Dataset) => readRawRows(record).length },
    {
      title: '更新时间',
      width: 190,
      render: (_: unknown, record: Dataset) => formatTime(record.updated_at || record.created_at),
    },
    {
      title: '操作',
      width: 170,
      render: (_: unknown, record: Dataset) => (
        <Space>
          <Button type="text" size="small" icon={<IconEdit />} onClick={() => startEdit(record)}>
            编辑
          </Button>
          <Popconfirm title="确认删除该数据集？" onOk={() => handleDelete(record.id)}>
            <Button type="text" size="small" status="danger" icon={<IconDelete />}>
              删除
            </Button>
          </Popconfirm>
        </Space>
      ),
    },
  ];

  const rowColumns: ColumnProps<EditableRow>[] = draft
    ? draft.columns.map((column, cellIndex) => ({
        title: column || `列${cellIndex + 1}`,
        width: 170,
        render: (_: unknown, record: EditableRow) => (
          <Input
            size="small"
            value={record.cells[cellIndex] ?? ''}
            placeholder={column || '值'}
            onChange={(value) => updateCell(record.id, cellIndex, value)}
          />
        ),
      }))
    : [];

  if (draft) {
    rowColumns.push({
      title: '',
      width: 60,
      render: (_: unknown, record: EditableRow) => (
        <Button
          size="mini"
          type="text"
          status="danger"
          icon={<IconDelete />}
          onClick={() => removeRow(record.id)}
          aria-label="删除行"
        />
      ),
    });
  }

  return (
    <Drawer width={880} title="数据集管理" visible={visible} onCancel={onClose} footer={null} unmountOnExit>
      {draft ? (
        <div className={styles.datasetEditor}>
          <div className={styles.datasetFieldRow}>
            <span className={styles.datasetFieldLabel}>名称</span>
            <Input
              value={draft.name}
              placeholder="数据集名称"
              onChange={(value) => updateDraft({ name: value })}
              style={{ width: 360 }}
            />
          </div>

          <div>
            <div className={styles.datasetSectionHeader}>
              <span className={styles.datasetSectionTitle}>列（{draft.columns.length}）</span>
              <Button size="small" type="outline" icon={<IconPlus />} onClick={addColumn}>
                添加列
              </Button>
            </div>
            {draft.columns.length === 0 ? (
              <div className={styles.datasetEmptyHint}>暂无列，请先添加列（CSV 首行即列名）</div>
            ) : (
              <div className={styles.datasetColumns}>
                {draft.columns.map((column, index) => (
                  <div className={styles.datasetColumnRow} key={index}>
                    <Input
                      size="small"
                      value={column}
                      placeholder={`列名 ${index + 1}`}
                      onChange={(value) => updateColumn(index, value)}
                    />
                    <Button
                      size="mini"
                      type="text"
                      status="danger"
                      icon={<IconDelete />}
                      onClick={() => removeColumn(index)}
                      aria-label="删除列"
                    />
                  </div>
                ))}
              </div>
            )}
          </div>

          <div>
            <div className={styles.datasetSectionHeader}>
              <span className={styles.datasetSectionTitle}>行（{draft.rows.length}）</span>
              <Button
                size="small"
                type="outline"
                icon={<IconPlus />}
                onClick={addRow}
                disabled={draft.columns.length === 0}
              >
                添加行
              </Button>
            </div>
            {draft.rows.length === 0 ? (
              <div className={styles.datasetEmptyHint}>该数据集暂无数据，点击「添加行」录入</div>
            ) : (
              <Table
                rowKey="id"
                size="small"
                border
                columns={rowColumns}
                data={draft.rows}
                pagination={false}
                scroll={{ x: 'max-content', y: 320 }}
              />
            )}
          </div>

          <div className={styles.datasetEditorFooter}>
            <Button onClick={() => setDraft(null)}>取消</Button>
            <Button type="primary" loading={saving} onClick={handleSave}>
              {draft.id != null ? '保存修改' : '创建数据集'}
            </Button>
          </div>
        </div>
      ) : (
        <div className={styles.datasetList}>
          <div className={styles.datasetListHeader}>
            <Space>
              <Button
                type="primary"
                icon={<IconPlus />}
                disabled={!projectId}
                onClick={startCreate}
              >
                新建数据集
              </Button>
              <Upload
                accept=".csv"
                autoUpload={false}
                showUploadList={false}
                fileList={csvFileList}
                onChange={handleCsvChange}
              >
                <Button icon={<IconUpload />} loading={importing} disabled={!projectId}>
                  导入 CSV
                </Button>
              </Upload>
            </Space>
            <Text type="secondary" style={{ fontSize: 12 }}>
              CSV 首行为表头，单文件 ≤2MB，最多 {DATASET_ROW_LIMIT} 行
            </Text>
          </div>

          {importError && (
            <Alert
              type="error"
              showIcon
              closable
              content={`导入失败：${importError}`}
              onClose={() => setImportError(null)}
              className={styles.datasetAlert}
            />
          )}

          {listError && (
            <div className={styles.datasetAlert}>
              <Alert type="error" showIcon content={`加载数据集失败：${listError}`} />
              <Button size="small" onClick={loadDatasets}>
                重试
              </Button>
            </div>
          )}

          <Spin loading={loading} style={{ width: '100%' }}>
            {datasets.length === 0 ? (
              <Empty description="暂无数据集，可新建或导入 CSV" />
            ) : (
              <Table
                rowKey="id"
                columns={listColumns}
                data={datasets}
                pagination={{ pageSize: 10, showTotal: true }}
                scroll={{ x: 'max-content' }}
              />
            )}
          </Spin>
        </div>
      )}
    </Drawer>
  );
};

export default DatasetPanel;
