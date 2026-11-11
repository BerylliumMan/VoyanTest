import React, { useCallback, useEffect, useState } from 'react';
import {
  Button, Drawer, Empty, Input, Message, Popconfirm, Select, Space, Tabs, Tag, Typography,
} from '@arco-design/web-react';
import axios from 'axios';

interface Member {
  id: number;
  username: string;
  nickname?: string | null;
  role?: string;
}

interface CommentItem {
  id: number;
  body: string;
  user_id: number;
  user_name: string;
  can_edit: boolean;
  created_at: string;
}

interface ReviewItem {
  id: number;
  case_id: number;
  requester_id: number;
  requester_name: string;
  reviewer_id: number;
  reviewer_name: string;
  status: string;
  note?: string | null;
  comment?: string | null;
  created_at: string;
  decided_at?: string | null;
}

interface Props {
  visible: boolean;
  caseId: number | null;
  caseName?: string;
  projectId?: number | null;
  members: Member[];
  currentUserId: number | null;
  onClose: () => void;
}

const REVIEW_LABELS: Record<string, string> = {
  pending: '待评审',
  approved: '通过',
  changes_requested: '需修改',
  rejected: '驳回',
  cancelled: '已取消',
};

const REVIEW_COLORS: Record<string, string> = {
  pending: 'orange',
  approved: 'green',
  changes_requested: 'gold',
  rejected: 'red',
  cancelled: 'gray',
};

const CollabDrawer: React.FC<Props> = ({
  visible, caseId, caseName, members, currentUserId, onClose,
}) => {
  const [tab, setTab] = useState('comments');
  const [comments, setComments] = useState<CommentItem[]>([]);
  const [reviews, setReviews] = useState<ReviewItem[]>([]);
  const [body, setBody] = useState('');
  const [note, setNote] = useState('');
  const [reviewerId, setReviewerId] = useState<number | null>(null);
  const [loading, setLoading] = useState(false);

  const load = useCallback(async () => {
    if (!caseId) return;
    setLoading(true);
    try {
      const [c, r] = await Promise.all([
        axios.get('/api/comments', { params: { entity_type: 'case', entity_id: caseId } }),
        axios.get('/api/case-reviews', { params: { case_id: caseId } }),
      ]);
      setComments(c.data?.items || []);
      setReviews(r.data?.items || []);
    } catch (e: any) {
      Message.error(e?.response?.data?.detail || '加载协作信息失败');
    } finally {
      setLoading(false);
    }
  }, [caseId]);

  useEffect(() => {
    if (visible) {
      setTab('comments');
      setBody('');
      setNote('');
      setReviewerId(null);
      load();
    }
  }, [visible, load]);

  const addComment = async () => {
    if (!caseId || !body.trim()) return;
    try {
      await axios.post('/api/comments', { entity_type: 'case', entity_id: caseId, body: body.trim() });
      setBody('');
      await load();
    } catch (e: any) {
      Message.error(e?.response?.data?.detail || '发表评论失败');
    }
  };

  const removeComment = async (id: number) => {
    try {
      await axios.delete(`/api/comments/${id}`);
      await load();
    } catch (e: any) {
      Message.error(e?.response?.data?.detail || '删除评论失败');
    }
  };

  const createReview = async () => {
    if (!caseId || !reviewerId) {
      Message.warning('请选择评审人');
      return;
    }
    try {
      await axios.post('/api/case-reviews', { case_id: caseId, reviewer_id: reviewerId, note: note.trim() || undefined });
      setNote('');
      setReviewerId(null);
      Message.success('评审已发起');
      await load();
    } catch (e: any) {
      Message.error(e?.response?.data?.detail || '发起评审失败');
    }
  };

  const decide = async (id: number, status: string) => {
    try {
      await axios.post(`/api/case-reviews/${id}/decision`, { status });
      await load();
    } catch (e: any) {
      Message.error(e?.response?.data?.detail || '评审操作失败');
    }
  };

  const cancelReview = async (id: number) => {
    try {
      await axios.post(`/api/case-reviews/${id}/cancel`);
      await load();
    } catch (e: any) {
      Message.error(e?.response?.data?.detail || '取消评审失败');
    }
  };

  const reviewOptions = members.filter((m) => m.id !== currentUserId && (m.role === 'admin' || m.role === 'tester'));

  return (
    <Drawer
      width={560}
      title={`协作 · ${caseName || `用例 #${caseId}`}`}
      visible={visible}
      onCancel={onClose}
      footer={null}
    >
      <Tabs activeTab={tab} onChange={setTab}>
        <Tabs.TabPane key="comments" title={`评论 ${comments.length ? `(${comments.length})` : ''}`}>
          <Space direction="vertical" style={{ width: '100%' }} size="medium">
            <Input.TextArea
              value={body}
              onChange={setBody}
              placeholder="输入评论，使用 @用户名 可提醒成员（1–5000 字）"
              maxLength={5000}
              autoSize={{ minRows: 2, maxRows: 6 }}
            />
            <Button type="primary" onClick={addComment} disabled={!body.trim()}>发表评论</Button>
            {comments.length === 0 ? <Empty description="暂无评论" /> : comments.map((c) => (
              <div key={c.id} style={{ borderBottom: '1px solid var(--color-border-2)', paddingBottom: 8 }}>
                <Space>
                  <Typography.Text bold>{c.user_name || `用户 #${c.user_id}`}</Typography.Text>
                  <Typography.Text type="secondary" style={{ fontSize: 12 }}>{c.created_at}</Typography.Text>
                  {c.can_edit && (
                    <Popconfirm title="删除这条评论？" onOk={() => removeComment(c.id)}>
                      <Button size="mini" type="text" status="danger">删除</Button>
                    </Popconfirm>
                  )}
                </Space>
                <div style={{ whiteSpace: 'pre-wrap', marginTop: 4 }}>{c.body}</div>
              </div>
            ))}
          </Space>
        </Tabs.TabPane>
        <Tabs.TabPane key="reviews" title={`评审 ${reviews.length ? `(${reviews.length})` : ''}`}>
          <Space direction="vertical" style={{ width: '100%' }} size="medium">
            <Space>
              <Select
                style={{ width: 200 }}
                placeholder="选择评审人"
                value={reviewerId ?? undefined}
                onChange={(v) => setReviewerId(v as number)}
                options={reviewOptions.map((m) => ({ label: m.nickname || m.username, value: m.id }))}
              />
              <Button type="primary" onClick={createReview}>发起评审</Button>
            </Space>
            <Input value={note} onChange={setNote} placeholder="评审说明（可选）" maxLength={2000} />
            {reviews.length === 0 ? <Empty description="暂未发起评审" /> : reviews.map((r) => (
              <div key={r.id} style={{ borderBottom: '1px solid var(--color-border-2)', paddingBottom: 8 }}>
                <Space wrap>
                  <Tag color={REVIEW_COLORS[r.status] || 'gray'}>{REVIEW_LABELS[r.status] || r.status}</Tag>
                  <Typography.Text>{r.requester_name} → {r.reviewer_name}</Typography.Text>
                  <Typography.Text type="secondary" style={{ fontSize: 12 }}>{r.created_at}</Typography.Text>
                </Space>
                {r.note && <div style={{ marginTop: 4 }}>说明：{r.note}</div>}
                {r.comment && <div style={{ marginTop: 4 }}>意见：{r.comment}</div>}
                {r.status === 'pending' && (
                  <Space style={{ marginTop: 6 }}>
                    {r.reviewer_id === currentUserId && (
                      <Space>
                        <Button size="mini" type="primary" onClick={() => decide(r.id, 'approved')}>通过</Button>
                        <Button size="mini" onClick={() => decide(r.id, 'changes_requested')}>需修改</Button>
                        <Button size="mini" status="danger" onClick={() => decide(r.id, 'rejected')}>驳回</Button>
                      </Space>
                    )}
                    {(r.requester_id === currentUserId || currentUserId == null) && (
                      <Popconfirm title="取消这次评审？" onOk={() => cancelReview(r.id)}>
                        <Button size="mini" type="text">取消</Button>
                      </Popconfirm>
                    )}
                  </Space>
                )}
              </div>
            ))}
          </Space>
        </Tabs.TabPane>
      </Tabs>
      {loading && <div style={{ color: 'var(--color-text-3)', marginTop: 8 }}>加载中…</div>}
    </Drawer>
  );
};

export default CollabDrawer;
