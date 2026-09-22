import React, { useCallback, useEffect, useState } from 'react';
import {
  Button,
  Checkbox,
  Drawer,
  Empty,
  Input,
  Message,
  Radio,
  Select,
  Space,
  Spin,
  Table,
  Tabs,
  Tag,
  Typography,
  Upload,
} from '@arco-design/web-react';
import type { UploadItem } from '@arco-design/web-react/es/Upload/interface';
import { IconUpload, IconThunderbolt, IconCheck } from '@arco-design/web-react/icon';
import { apiRequest } from '@/utils/apiRequest';
import {
  ApiDefinition,
  DEFAULT_GENERATE_OPTIONS,
  GenerateOptions,
  GenerateResponse,
  GeneratedCase,
  ImportResult,
} from '../types';
import styles from '../style/index.module.less';

const { Title, Text } = Typography;
const TabPane = Tabs.TabPane;

interface DefinitionDrawerProps {
  visible: boolean;
  onClose: () => void;
  projectId: number | null;
  /** 导入成功后回调（刷新左侧定义树） */
  onImported?: () => void;
  /** 生成并导入成功后回调 */
  onGenerated?: () => void;
}

/**
 * 导入 / 生成抽屉：
 * - 导入：文件上传（multipart）或 Swagger URL
 * - 定义列表：筛选 + 勾选
 * - 生成：POST /api/api-test/generate → 草案预览 → 导入
 */
const DefinitionDrawer: React.FC<DefinitionDrawerProps> = ({
  visible,
  onClose,
  projectId,
  onImported,
  onGenerated,
}) => {
  const [activeTab, setActiveTab] = useState('import');

  // ---- 导入 ----
  const [fileList, setFileList] = useState<UploadItem[]>([]);
  const [swaggerUrl, setSwaggerUrl] = useState('');
  const [onConflict, setOnConflict] = useState<'skip' | 'overwrite'>('skip');
  const [importing, setImporting] = useState(false);
  const [importResult, setImportResult] = useState<ImportResult | null>(null);

  // ---- 定义列表 ----
  const [definitions, setDefinitions] = useState<ApiDefinition[]>([]);
  const [defLoading, setDefLoading] = useState(false);
  const [keyword, setKeyword] = useState('');
  const [selectedIds, setSelectedIds] = useState<number[]>([]);

  // ---- 生成 ----
  const [options, setOptions] = useState<GenerateOptions>(DEFAULT_GENERATE_OPTIONS);
  const [generating, setGenerating] = useState(false);
  const [genResult, setGenResult] = useState<GenerateResponse | null>(null);
  const [selectedDraftIds, setSelectedDraftIds] = useState<string[]>([]);
  const [importingDrafts, setImportingDrafts] = useState(false);

  const loadDefinitions = useCallback(async () => {
    if (!projectId) return;
    setDefLoading(true);
    try {
      const data = await apiRequest<{ total: number; items: ApiDefinition[] }>({
        method: 'GET',
        url: '/api/api-test/definitions',
        params: { project_id: projectId, page_size: 500 },
      });
      setDefinitions(data.items || []);
    } catch {
      setDefinitions([]);
    } finally {
      setDefLoading(false);
    }
  }, [projectId]);

  useEffect(() => {
    if (visible && projectId) {
      loadDefinitions();
    }
  }, [visible, projectId, loadDefinitions]);

  const resetImport = () => {
    setFileList([]);
    setSwaggerUrl('');
    setImportResult(null);
  };

  const handleImportFile = async () => {
    if (!projectId) {
      Message.warning('请先选择项目');
      return;
    }
    if (fileList.length === 0) {
      Message.warning('请选择要导入的文档');
      return;
    }
    setImporting(true);
    try {
      const formData = new FormData();
      formData.append('project_id', String(projectId));
      formData.append('on_conflict', onConflict);
      const file = fileList[0].originFile;
      if (file) {
        formData.append('file', file);
      }
      const data = await apiRequest<ImportResult>(
        {
          method: 'POST',
          url: '/api/api-test/import',
          data: formData,
          headers: { 'Content-Type': 'multipart/form-data' },
        },
        { successMessage: '导入成功' }
      );
      setImportResult(data);
      resetImport();
      onImported?.();
    } catch {
      /* apiRequest 已弹错误 */
    } finally {
      setImporting(false);
    }
  };

  const handleImportUrl = async () => {
    if (!projectId) {
      Message.warning('请先选择项目');
      return;
    }
    if (!swaggerUrl.trim()) {
      Message.warning('请输入 Swagger/OpenAPI URL');
      return;
    }
    setImporting(true);
    try {
      const data = await apiRequest<ImportResult>(
        {
          method: 'POST',
          url: '/api/api-test/import',
          data: { swagger_url: swaggerUrl.trim(), project_id: projectId, on_conflict: onConflict },
        },
        { successMessage: '导入成功' }
      );
      setImportResult(data);
      resetImport();
      onImported?.();
    } catch {
      /* apiRequest 已弹错误 */
    } finally {
      setImporting(false);
    }
  };

  const filteredDefinitions = definitions.filter((d) => {
    if (!keyword.trim()) return true;
    const kw = keyword.toLowerCase();
    return (
      d.name.toLowerCase().includes(kw) ||
      d.path.toLowerCase().includes(kw) ||
      d.method.toLowerCase().includes(kw) ||
      (d.tags || '').toLowerCase().includes(kw)
    );
  });

  const handleGenerate = async () => {
    if (!projectId) {
      Message.warning('请先选择项目');
      return;
    }
    if (selectedIds.length === 0) {
      Message.warning('请先勾选要生成用例的接口');
      return;
    }
    setGenerating(true);
    setGenResult(null);
    try {
      const data = await apiRequest<GenerateResponse>(
        {
          method: 'POST',
          url: '/api/api-test/generate',
          data: {
            project_id: projectId,
            definition_ids: selectedIds,
            options,
          },
        },
        { showSuccess: false }
      );
      setGenResult(data);
      setSelectedDraftIds((data.cases || []).map((c) => c.draft_id));
    } catch {
      /* apiRequest 已弹错误 */
    } finally {
      setGenerating(false);
    }
  };

  const handleImportDrafts = async () => {
    if (!genResult) return;
    if (selectedDraftIds.length === 0) {
      Message.warning('请选择要导入的草案');
      return;
    }
    setImportingDrafts(true);
    try {
      const data = await apiRequest<{ created: number[]; skipped: Array<{ draft_id: string; reason: string }> }>(
        {
          method: 'POST',
          url: `/api/api-test/generate/${genResult.session_id}/import`,
          data: { draft_ids: selectedDraftIds },
        },
        { showSuccess: false }
      );
      Message.success(`已导入 ${data?.created?.length ?? 0} 条用例`);
      onGenerated?.();
      setGenResult(null);
      setSelectedDraftIds([]);
      setActiveTab('definitions');
    } catch {
      /* apiRequest 已弹错误 */
    } finally {
      setImportingDrafts(false);
    }
  };

  const definitionColumns = [
    { title: '方法', dataIndex: 'method', width: 80, render: (v: string) => <Tag color="arcoblue">{v}</Tag> },
    { title: '路径', dataIndex: 'path', ellipsis: true },
    { title: '名称', dataIndex: 'name', ellipsis: true },
  ];

  const draftColumns = [
    { title: '名称', dataIndex: 'name', ellipsis: true },
    { title: '优先级', dataIndex: 'priority', width: 90, render: (v: string) => <Tag color={v === '高' ? 'red' : v === '中' ? 'orange' : 'green'}>{v}</Tag> },
    { title: '备注', dataIndex: 'notes', ellipsis: true, render: (v?: string) => v || '-' },
  ];

  return (
    <Drawer
      width={720}
      title="导入接口文档 / 生成用例"
      visible={visible}
      onCancel={onClose}
      footer={null}
      unmountOnExit
    >
      <Tabs activeTab={activeTab} onChange={setActiveTab}>
        {/* ===== 导入 ===== */}
        <TabPane key="import" title="导入文档">
          <Space direction="vertical" style={{ width: '100%' }} size="large">
            <div>
              <Title heading={6}>上传文件（OpenAPI3 / Swagger2 / Postman v2.1）</Title>
              <Upload
                drag
                accept=".json,.yaml,.yml"
                fileList={fileList}
                onChange={setFileList}
                autoUpload={false}
                limit={1}
                tip="支持 .json / .yaml 格式"
              />
              <div className={styles.drawerRow}>
                <span className={styles.drawerLabel}>冲突处理：</span>
                <Radio.Group
                  type="button"
                  value={onConflict}
                  onChange={(v) => setOnConflict(v as 'skip' | 'overwrite')}
                >
                  <Radio value="skip">跳过</Radio>
                  <Radio value="overwrite">覆盖</Radio>
                </Radio.Group>
                <Button
                  type="primary"
                  icon={<IconUpload />}
                  loading={importing}
                  onClick={handleImportFile}
                  disabled={fileList.length === 0}
                >
                  导入文件
                </Button>
              </div>
            </div>

            <div>
              <Title heading={6}>从 URL 导入</Title>
              <div className={styles.drawerRow}>
                <Input
                  placeholder="https://example.com/swagger.json"
                  value={swaggerUrl}
                  onChange={setSwaggerUrl}
                  style={{ flex: 1 }}
                />
                <Button
                  type="primary"
                  icon={<IconUpload />}
                  loading={importing}
                  onClick={handleImportUrl}
                  disabled={!swaggerUrl.trim()}
                >
                  导入 URL
                </Button>
              </div>
            </div>

            {importResult && (
              <div className={styles.importResult}>
                <Title heading={6}>导入结果</Title>
                <Space>
                  <Tag color="arcoblue">来源 {importResult.source}</Tag>
                  <Tag>总数 {importResult.total_operations}</Tag>
                  <Tag color="green">新建 {importResult.created}</Tag>
                  <Tag color="orange">更新 {importResult.updated}</Tag>
                  <Tag color="gray">跳过 {importResult.skipped}</Tag>
                </Space>
              </div>
            )}
          </Space>
        </TabPane>

        {/* ===== 定义列表 ===== */}
        <TabPane key="definitions" title="接口定义">
          <div className={styles.drawerRow} style={{ marginBottom: 12 }}>
            <Input.Search
              placeholder="按名称 / 路径 / 方法筛选"
              value={keyword}
              onChange={setKeyword}
              allowClear
              style={{ flex: 1 }}
            />
            <Button size="small" onClick={loadDefinitions} loading={defLoading}>
              刷新
            </Button>
          </div>
          <Spin loading={defLoading}>
            {filteredDefinitions.length === 0 ? (
              <Empty description="暂无接口定义，请先导入文档" />
            ) : (
              <Table
                rowKey="id"
                columns={definitionColumns}
                data={filteredDefinitions}
                pagination={{ pageSize: 10, showTotal: true }}
                rowSelection={{
                  type: 'checkbox',
                  selectedRowKeys: selectedIds,
                  onChange: (keys) => setSelectedIds(keys.map(Number)),
                }}
                scroll={{ y: 320 }}
              />
            )}
          </Spin>
        </TabPane>

        {/* ===== 生成 ===== */}
        <TabPane key="generate" title="生成用例">
          <Space direction="vertical" style={{ width: '100%' }} size="large">
            <div>
              <Title heading={6}>生成选项</Title>
              <div className={styles.generateOptions}>
                <Checkbox
                  checked={options.normal}
                  onChange={(v) => setOptions({ ...options, normal: v })}
                >
                  正常场景
                </Checkbox>
                <Checkbox
                  checked={options.boundary}
                  onChange={(v) => setOptions({ ...options, boundary: v })}
                >
                  边界场景
                </Checkbox>
                <Checkbox
                  checked={options.missing_required}
                  onChange={(v) => setOptions({ ...options, missing_required: v })}
                >
                  缺必填参数
                </Checkbox>
                <Checkbox
                  checked={options.type_error}
                  onChange={(v) => setOptions({ ...options, type_error: v })}
                >
                  类型错误
                </Checkbox>
                <Checkbox
                  checked={options.auth_fail}
                  onChange={(v) => setOptions({ ...options, auth_fail: v })}
                >
                  鉴权失败
                </Checkbox>
                <Checkbox
                  checked={options.use_llm}
                  onChange={(v) => setOptions({ ...options, use_llm: v })}
                >
                  使用 LLM 生成
                </Checkbox>
              </div>
              <div className={styles.drawerRow}>
                <span className={styles.drawerLabel}>每接口最多用例数：</span>
                <Select
                  value={options.max_cases_per_operation}
                  onChange={(v) => setOptions({ ...options, max_cases_per_operation: v })}
                  style={{ width: 120 }}
                  options={[1, 2, 3, 5, 10].map((n) => ({ label: String(n), value: n }))}
                />
                <Button
                  type="primary"
                  icon={<IconThunderbolt />}
                  loading={generating}
                  onClick={handleGenerate}
                  disabled={selectedIds.length === 0}
                >
                  生成用例（已选 {selectedIds.length} 个接口）
                </Button>
              </div>
              <Text type="secondary" style={{ fontSize: 12 }}>
                未勾选接口时请先在「接口定义」页勾选
              </Text>
            </div>

            {genResult && (
              <div>
                <Title heading={6}>
                  草案预览（{genResult.cases.length} 条）
                </Title>
                <Table
                  rowKey="draft_id"
                  columns={draftColumns}
                  data={genResult.cases}
                  pagination={{ pageSize: 10, showTotal: true }}
                  rowSelection={{
                    type: 'checkbox',
                    selectedRowKeys: selectedDraftIds,
                    onChange: (keys) => setSelectedDraftIds(keys.map(String)),
                  }}
                  scroll={{ y: 320 }}
                />
                <div className={styles.drawerRow} style={{ marginTop: 12 }}>
                  <Button
                    type="primary"
                    status="success"
                    icon={<IconCheck />}
                    loading={importingDrafts}
                    onClick={handleImportDrafts}
                    disabled={selectedDraftIds.length === 0}
                  >
                    导入选中草案（{selectedDraftIds.length}）
                  </Button>
                </div>
              </div>
            )}
          </Space>
        </TabPane>
      </Tabs>
    </Drawer>
  );
};

export default DefinitionDrawer;