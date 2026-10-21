import React, { useMemo, useRef, useState } from 'react';
import {
  Button, Input, InputNumber, Message, Select, Space, Switch, Tag, Typography,
} from '@arco-design/web-react';
import axios from 'axios';
import { Step } from '../types';

export interface ParameterDef {
  name: string;
  description?: string;
}

export interface DatasetBinding {
  dataset_id: number | null;
  dataset_mode: 'sequential' | 'random' | 'loop';
  loop_count: number;
  fail_fast: boolean;
}

export interface DatasetOption {
  id: number;
  name: string;
  columns: string[];
  rows: Record<string, unknown>[];
}

interface Props {
  steps: Step[];
  parameters: ParameterDef[];
  onParametersChange: (next: ParameterDef[]) => void;
  datasets: DatasetOption[];
  binding: DatasetBinding;
  onBindingChange: (next: DatasetBinding) => void;
  projectId?: number | null;
  onDatasetCreated?: (dataset: DatasetOption) => void;
  t: Record<string, string>;
}

const PARAM_RE = /\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}/g;

function collectText(step: Step): string[] {
  return [
    step.description || '',
    step.parsed_result || '',
    step.structured_step ? JSON.stringify(step.structured_step) : '',
  ];
}

const ParameterPanel: React.FC<Props> = ({
  steps, parameters, onParametersChange, datasets, binding, onBindingChange, projectId, onDatasetCreated, t,
}) => {
  const fileRef = useRef<HTMLInputElement | null>(null);
  const [importing, setImporting] = useState(false);
  const discovered = useMemo(() => {
    const seen: string[] = [];
    steps.forEach((step) => {
      collectText(step).forEach((text) => {
        let match: RegExpExecArray | null;
        const re = new RegExp(PARAM_RE.source, 'g');
        while ((match = re.exec(text)) !== null) {
          if (!seen.includes(match[1])) seen.push(match[1]);
        }
      });
    });
    return seen;
  }, [steps]);

  const declared = parameters.map((p) => p.name);
  const missing = discovered.filter((name) => !declared.includes(name));
  const selectedDataset = datasets.find((d) => d.id === binding.dataset_id);
  const rows = selectedDataset ? selectedDataset.rows.length : 0;
  const groups = binding.dataset_mode === 'loop' ? rows * (binding.loop_count || 1) : rows;

  const updateDeclared = (index: number, patch: Partial<ParameterDef>) => {
    const next = [...parameters];
    next[index] = { ...next[index], ...patch };
    onParametersChange(next);
  };

  const handleCsvFile = async (file: File | null) => {
    if (!file || !projectId) return;
    setImporting(true);
    try {
      const form = new FormData();
      form.append('file', file);
      const parsed = await axios.post('/api/api-test/datasets/import', form, {
        params: { purpose: 'ui' },
        headers: { 'Content-Type': 'multipart/form-data' },
      });
      const created = await axios.post('/api/api-test/datasets', {
        project_id: projectId,
        name: file.name.replace(/\.csv$/i, ''),
        columns: parsed.data.columns,
        rows: parsed.data.rows,
        source: 'csv',
        purpose: 'ui',
      });
      const dataset = created.data as DatasetOption;
      onDatasetCreated?.(dataset);
      onBindingChange({ ...binding, dataset_id: dataset.id });
      Message.success(t['param.dataset.created'] || `已创建数据集（${dataset.rows.length} 行）`);
    } catch (e: unknown) {
      const err = e as { response?: { data?: { detail?: string } } };
      Message.error(err.response?.data?.detail || t['param.dataset.import_failed'] || '导入 CSV 失败');
    } finally {
      setImporting(false);
      if (fileRef.current) fileRef.current.value = '';
    }
  };

  return (
    <div
      data-testid="parameter-panel"
      style={{
        marginBottom: 16,
        padding: '10px 12px',
        background: 'var(--color-fill-2)',
        borderRadius: 6,
      }}
    >
      <div style={{ fontWeight: 600, marginBottom: 8 }}>
        {t['param.title'] || '参数与数据集'}
      </div>
      <div style={{ marginBottom: 8 }}>
        <Typography.Text style={{ marginRight: 8 }}>
          {t['param.discovered'] || '步骤中引用'}
        </Typography.Text>
        {discovered.length === 0 ? (
          <Typography.Text type="secondary">
            {t['param.none'] || '未发现 {{参数}}（可在步骤描述中直接书写）'}
          </Typography.Text>
        ) : (
          discovered.map((name) => (
            <Tag
              key={name}
              color={declared.includes(name) ? 'green' : 'red'}
              style={{ marginRight: 6 }}
            >
              {`{{${name}}}`}{declared.includes(name) ? '' : ` (${t['param.undeclared'] || '未声明'})`}
            </Tag>
          ))
        )}
      </div>
      {parameters.map((p, index) => (
        <Space key={index} style={{ display: 'flex', marginBottom: 6 }}>
          <Input
            style={{ width: 160 }}
            value={p.name}
            placeholder={t['param.name'] || '参数名'}
            onChange={(value) => updateDeclared(index, { name: value })}
          />
          <Input
            style={{ width: 220 }}
            value={p.description || ''}
            placeholder={t['param.desc'] || '说明（可选）'}
            onChange={(value) => updateDeclared(index, { description: value })}
          />
          <Button
            size="mini"
            status="danger"
            onClick={() => onParametersChange(parameters.filter((_, i) => i !== index))}
          >
            {t['delete'] || '删除'}
          </Button>
        </Space>
      ))}
      <Button
        size="mini"
        type="outline"
        onClick={() => onParametersChange([...parameters, { name: '', description: '' }])}
        style={{ marginBottom: 12 }}
      >
        {t['param.add'] || '添加参数声明'}
      </Button>
      {missing.length > 0 ? (
        <div style={{ color: 'rgb(var(--danger-6))', fontSize: 12, marginBottom: 8 }}>
          {`${t['param.missing'] || '以下参数未声明，执行前需补充'}：${missing.join('、')}`}
        </div>
      ) : null}

      <div style={{ fontWeight: 600, margin: '8px 0' }}>
        {t['param.dataset'] || '数据集绑定'}
      </div>
      <Space wrap>
        <Select
          allowClear
          style={{ width: 220 }}
          placeholder={t['param.dataset.select'] || '选择 UI 数据集'}
          value={binding.dataset_id ?? undefined}
          onChange={(value) => onBindingChange({ ...binding, dataset_id: (value as number) ?? null })}
          options={datasets.map((d) => ({
            label: `${d.name}（${d.rows.length} 行）`,
            value: d.id,
          }))}
        />
        <input
          ref={fileRef}
          type="file"
          accept=".csv,text/csv"
          style={{ display: 'none' }}
          onChange={(e) => handleCsvFile(e.target.files?.[0] || null)}
        />
        <Button size="mini" loading={importing} onClick={() => fileRef.current?.click()}>
          {t['param.dataset.import'] || '导入 CSV 新建'}
        </Button>
        <Select
          style={{ width: 130 }}
          value={binding.dataset_mode}
          onChange={(value) => onBindingChange({ ...binding, dataset_mode: value as DatasetBinding['dataset_mode'] })}
          options={[
            { label: t['param.mode.sequential'] || '顺序', value: 'sequential' },
            { label: t['param.mode.random'] || '随机', value: 'random' },
            { label: t['param.mode.loop'] || '循环', value: 'loop' },
          ]}
        />
        {binding.dataset_mode === 'loop' ? (
          <InputNumber
            min={1}
            max={100}
            value={binding.loop_count}
            onChange={(value) => onBindingChange({ ...binding, loop_count: Number(value) || 1 })}
            style={{ width: 110 }}
          />
        ) : null}
        <span style={{ fontSize: 12 }}>
          {t['param.fail_fast'] || '首组失败即停'}
          <Switch
            size="small"
            style={{ marginLeft: 6 }}
            checked={binding.fail_fast}
            onChange={(value) => onBindingChange({ ...binding, fail_fast: value })}
          />
        </span>
        {binding.dataset_id ? (
          <Tag color={groups > 100 ? 'red' : 'arcoblue'}>
            {`${t['param.groups'] || '本次执行'} ${groups} ${t['param.groups.unit'] || '组'}`}
          </Tag>
        ) : null}
      </Space>
    </div>
  );
};

export default ParameterPanel;
