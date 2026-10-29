import React, { useEffect, useState } from 'react';
import axios from 'axios';
import { Button, Input, Message, Modal, Space, Table, Tag } from '@arco-design/web-react';

interface Props {
  visible: boolean;
  projectId: number | null;
  targetType: 'case' | 'draft';
  targetIds: number[];
  onClose: () => void;
  onApplied?: () => void;
}

export default function CaseReviewModal({ visible, projectId, targetType, targetIds, onClose, onApplied }: Props) {
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<any>(null);
  const [error, setError] = useState('');
  const [applyItem, setApplyItem] = useState<any>(null);
  const [applyName, setApplyName] = useState('');
  const [applySteps, setApplySteps] = useState('');
  const [applying, setApplying] = useState(false);

  const run = async () => {
    if (!projectId || targetIds.length === 0) return;
    setLoading(true);
    setError('');
    setResult(null);
    try {
      const res = await axios.post('/api/quality/case-reviews', {
        project_id: projectId,
        target_type: targetType,
        target_ids: targetIds,
      });
      let data = res.data;
      if (res.status === 202 && data?.task_id) {
        for (let i = 0; i < 120; i += 1) {
          await new Promise((r) => setTimeout(r, 1000));
          const task = await axios.get(`/api/quality/tasks/${data.task_id}`);
          if (task.data.status === 'completed') {
            data = { analysis_id: task.data.analysis_id, result: task.data.result };
            break;
          }
          if (task.data.status === 'failed' || task.data.status === 'stopped') {
            throw new Error(task.data.error || '评审任务已停止');
          }
        }
      }
      setResult(data);
    } catch (err: any) {
      setError(err?.response?.data?.detail || err?.message || '评审失败');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    if (visible) run();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [visible, projectId, targetIds.join(','), targetType]);

  const openApply = (item: any) => {
    setApplyItem(item);
    setApplyName(item.name || '');
    setApplySteps((item.issues || []).length ? '' : '');
  };

  const submitApply = async () => {
    if (!result?.analysis_id || !applyItem) return;
    setApplying(true);
    try {
      const lines = applySteps
        .split('\n')
        .map((line) => line.replace(/^\s*\d+[.、]\s*/, '').trim())
        .filter(Boolean);
      await axios.post(`/api/quality/case-reviews/${result.analysis_id}/apply`, {
        target_type: applyItem.target_type || targetType,
        target_id: applyItem.target_id,
        base_hash: applyItem.base_hash,
        name: applyName || undefined,
        steps: lines.length ? lines : undefined,
      });
      Message.success('已应用评审建议');
      setApplyItem(null);
      onApplied?.();
    } catch (err: any) {
      Message.error(err?.response?.data?.detail || '应用失败');
    } finally {
      setApplying(false);
    }
  };

  return (
    <>
      <Modal
        title="AI 用例评审"
        visible={visible}
        onCancel={onClose}
        footer={<Button onClick={onClose}>关闭</Button>}
        style={{ width: 960 }}
      >
        {error && <div style={{ color: 'var(--color-danger-6)', marginBottom: 8 }}>{error}</div>}
        {loading && <div style={{ color: 'var(--color-text-3)' }}>评审中…</div>}
        {result?.result?.summary && <div style={{ fontSize: 13, marginBottom: 8 }}>{result.result.summary}</div>}
        <Table
          rowKey={(r: any) => `${r.target_type}-${r.target_id}`}
          size="small"
          loading={loading}
          pagination={{ pageSize: 8 }}
          data={result?.result?.items || []}
          columns={[
            { title: '用例', dataIndex: 'name' },
            {
              title: '评分',
              dataIndex: 'score',
              width: 80,
              render: (v: number) => (
                <Tag color={v < 60 ? 'red' : v < 80 ? 'orange' : 'green'}>{v}</Tag>
              ),
            },
            {
              title: '问题与建议',
              render: (_: any, r: any) => (
                <div>
                  {(r.issues || []).map((issue: any, i: number) => (
                    <div key={i} style={{ fontSize: 12, marginBottom: 3 }}>
                      <Tag size="small" color={issue.severity === 'high' ? 'red' : issue.severity === 'medium' ? 'orange' : 'gray'}>
                        {issue.type}
                      </Tag>
                      {issue.detail}
                      {issue.suggestion ? <span style={{ color: 'var(--color-text-3)' }}> → {issue.suggestion}</span> : null}
                    </div>
                  ))}
                </div>
              ),
            },
            {
              title: '操作',
              width: 90,
              render: (_: any, r: any) => (
                <Button size="mini" onClick={() => openApply(r)}>应用修改</Button>
              ),
            },
          ]}
        />
      </Modal>

      <Modal
        title="应用评审建议"
        visible={!!applyItem}
        onCancel={() => setApplyItem(null)}
        onOk={submitApply}
        confirmLoading={applying}
        okText="确认并保存"
      >
        <Space direction="vertical" style={{ width: '100%' }}>
          <div>用例名称</div>
          <Input value={applyName} onChange={setApplyName} />
          <div>步骤（可选，每行一步；留空则不改步骤）</div>
          <Input.TextArea value={applySteps} onChange={setApplySteps} autoSize={{ minRows: 6, maxRows: 16 }} />
          <div style={{ fontSize: 12, color: 'var(--color-text-3)' }}>未确认前不会写入。</div>
        </Space>
      </Modal>
    </>
  );
}
