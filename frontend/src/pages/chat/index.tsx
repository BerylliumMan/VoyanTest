import React, { useCallback, useEffect, useRef, useState } from 'react';
import {
  Button,
  Checkbox,
  Empty,
  Input,
  Message,
  Modal,
  Popconfirm,
  Progress,
  Select,
  Spin,
  Tag,
  Typography,
} from '@arco-design/web-react';
import {
  IconDelete,
  IconDownload,
  IconImport,
  IconList,
  IconPlus,
  IconRefresh,
  IconSend,
  IconUpload,
} from '@arco-design/web-react/icon';
import axios from 'axios';
import styles from './style/index.module.less';

const { Text } = Typography;

interface Conversation {
  id: number;
  title: string;
  project_id: number | null;
  assistant_agent_id: number | null;
  status: string;
  refs?: Array<Record<string, unknown>>;
  updated_at?: string | null;
}

interface CardPayload {
  type: string;
  status?: string;
  gen_session_id?: string;
  case_kind?: string;
  progress?: number;
  progress_message?: string;
  items?: Array<{ draft_id: number; title: string; steps: string[]; expected?: string; state?: string | null }>;
  imported_case_ids?: number[];
  error?: string;
  last_changes?: unknown[];
  [key: string]: unknown;
}

interface ChatMessage {
  id: number;
  role: 'user' | 'assistant';
  content: string;
  cards: CardPayload[];
  actions: Array<Record<string, unknown>>;
  refs?: Array<Record<string, unknown>>;
  created_at?: string;
}

interface Project {
  id: number;
  name: string;
}

const DRAFT_STATUS_TAG: Record<string, { color: string; text: string }> = {
  generating: { color: 'blue', text: '生成中' },
  draft: { color: 'green', text: '草稿' },
  partial: { color: 'orange', text: '部分导入' },
  imported: { color: 'gray', text: '已导入' },
  failed: { color: 'red', text: '生成失败' },
};

const ChatPage: React.FC = () => {
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [activeId, setActiveId] = useState<number | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState('');
  const [sending, setSending] = useState(false);
  const [loadingMsgs, setLoadingMsgs] = useState(false);
  const [projects, setProjects] = useState<Project[]>([]);
  const [createVisible, setCreateVisible] = useState(false);
  const [createProjectId, setCreateProjectId] = useState<number | undefined>(undefined);
  const [selectedDrafts, setSelectedDrafts] = useState<Record<string, number[]>>({});
  const [attachments, setAttachments] = useState<Array<{ file_ref: string; filename: string }>>([]);
  const [uploading, setUploading] = useState(false);
  const [refCases, setRefCases] = useState<Array<{ id: number; name: string; case_kind?: string }>>([]);
  const [chatEnabled, setChatEnabled] = useState<boolean>(true);
  const [refPickerVisible, setRefPickerVisible] = useState(false);
  const [refSearch, setRefSearch] = useState('');
  const [refOptions, setRefOptions] = useState<Array<{ id: number; name: string }>>([]);
  const bottomRef = useRef<HTMLDivElement | null>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  const loadConversations = useCallback(async () => {
    try {
      const r = await axios.get('/api/chat/conversations');
      setConversations(r.data.items || []);
      return r.data.items as Conversation[];
    } catch {
      return [];
    }
  }, []);

  const loadMessages = useCallback(async (cid: number) => {
    setLoadingMsgs(true);
    try {
      const r = await axios.get(`/api/chat/conversations/${cid}/messages`, { params: { limit: 200 } });
      setMessages(r.data.items || []);
    } finally {
      setLoadingMsgs(false);
    }
  }, []);

  useEffect(() => {
    axios.get('/api/projects/').then((r) => setProjects(r.data || [])).catch(() => undefined);
    axios.get('/api/chat/status').then((r) => setChatEnabled(r.data?.enabled !== false)).catch(() => undefined);
    loadConversations().then((items) => {
      if (items.length) {
        setActiveId(items[0].id);
      }
    });
  }, [loadConversations]);

  useEffect(() => {
    if (activeId == null) return;
    loadMessages(activeId);
  }, [activeId, loadMessages]);

  // WS：实时消息与卡片更新；断线重连后 REST 补偿
  useEffect(() => {
    if (activeId == null) return undefined;
    const proto = window.location.protocol === 'https:' ? 'wss' : 'ws';
    const ws = new WebSocket(`${proto}://${window.location.host}/ws/conversations/${activeId}`);
    wsRef.current = ws;
    ws.onmessage = (ev) => {
      try {
        const data = JSON.parse(ev.data);
        if (data.type === 'message.created' || data.type === 'message.updated') {
          const msg = data.message as ChatMessage;
          setMessages((prev) => {
            const idx = prev.findIndex((m) => m.id === msg.id);
            if (idx >= 0) {
              const copy = [...prev];
              copy[idx] = msg;
              return copy;
            }
            return [...prev, msg];
          });
        } else if (data.type === 'message.deleted') {
          setMessages((prev) => prev.filter((m) => m.id !== data.message_id));
        }
      } catch {
        /* ignore */
      }
    };
    ws.onclose = () => {
      if (wsRef.current === ws) {
        loadMessages(activeId);
      }
    };
    const ping = window.setInterval(() => {
      try {
        ws.send(JSON.stringify({ type: 'ping' }));
      } catch {
        /* ignore */
      }
    }, 25000);
    return () => {
      window.clearInterval(ping);
      ws.close();
      wsRef.current = null;
    };
  }, [activeId, loadMessages]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages.length]);

  // US6：/chat?case_id= 或 ?batch_id= → 新建对话（绑定项目 + 注入引用 + 预填指令）
  const urlHandledRef = useRef(false);
  useEffect(() => {
    if (urlHandledRef.current) return;
    urlHandledRef.current = true;
    const params = new URLSearchParams(window.location.search);
    const caseId = Number(params.get('case_id'));
    const batchId = Number(params.get('batch_id'));
    const prompt = params.get('prompt') || '';
    const hasCase = Number.isFinite(caseId) && caseId > 0;
    const hasBatch = Number.isFinite(batchId) && batchId > 0;
    if (!hasCase && !hasBatch) return;
    window.history.replaceState({}, '', window.location.pathname);
    (async () => {
      try {
        const refs: Array<Record<string, unknown>> = [];
        let projectId: number | undefined;
        if (hasCase) {
          const cr = await axios.get(`/api/testcases/${caseId}`);
          projectId = cr.data.project_id;
          refs.push({ type: 'case', id: caseId, label: cr.data.name, case_kind: cr.data.case_kind || 'ui' });
        }
        if (hasBatch) {
          const br = await axios.get(`/api/reports/batches/${batchId}`);
          projectId = projectId ?? br.data.project_id;
          refs.push({ type: 'batch', id: batchId, label: `批次 #${batchId}` });
        }
        if (!projectId) {
          Message.warning('未找到对应资源');
          return;
        }
        const title = refs.length ? String(refs[0].label || '').slice(0, 40) : undefined;
        const conv = await axios.post('/api/chat/conversations', { project_id: projectId, refs, title });
        await loadConversations();
        setActiveId(conv.data.id);
        if (prompt) setInput(prompt);
        else if (hasBatch) setInput(`请分析批次 #${batchId} 的失败原因`);
      } catch (e: any) {
        Message.error(e?.response?.data?.detail || '打开对话失败');
      }
    })();
  }, [loadConversations]);

  const createConversation = async () => {
    if (!createProjectId) {
      Message.warning('请选择项目');
      return;
    }
    try {
      const r = await axios.post('/api/chat/conversations', { project_id: createProjectId });
      setCreateVisible(false);
      setCreateProjectId(undefined);
      await loadConversations();
      setActiveId(r.data.id);
      Message.success('对话已创建');
    } catch (e: any) {
      Message.error(e?.response?.data?.detail || '创建失败');
    }
  };

  const deleteConversation = async (id: number) => {
    try {
      await axios.delete(`/api/chat/conversations/${id}`);
      const items = await loadConversations();
      if (activeId === id) {
        setActiveId(items.length ? items[0].id : null);
        setMessages([]);
      }
    } catch {
      Message.error('删除失败');
    }
  };

  const send = async () => {
    if (activeId == null) return;
    const text = input.trim();
    if (!text && attachments.length === 0) return;
    setSending(true);
    try {
      const r = await axios.post(`/api/chat/conversations/${activeId}/messages`, {
        content: text,
        refs: refCases.length
          ? refCases.map((c) => ({ type: 'case', id: c.id, label: c.name, case_kind: (c as any).case_kind || 'ui' }))
          : undefined,
        attachments: attachments.length ? attachments : undefined,
      });
      setInput('');
      setAttachments([]);
      setRefCases([]);
      const { user_message, assistant_message } = r.data;
      setMessages((prev) => [...prev, user_message, assistant_message]);
    } catch (e: any) {
      Message.error(e?.response?.data?.detail || '发送失败');
    } finally {
      setSending(false);
    }
  };

  const sendPrompt = async (text: string) => {
    if (activeId == null || !text.trim()) return;
    setSending(true);
    try {
      const r = await axios.post(`/api/chat/conversations/${activeId}/messages`, { content: text });
      setMessages((prev) => [...prev, r.data.user_message, r.data.assistant_message]);
    } catch (e: any) {
      Message.error(e?.response?.data?.detail || '发送失败');
    } finally {
      setSending(false);
    }
  };

  const exportConversation = () => {
    if (activeId == null) return;
    window.open(`/api/chat/conversations/${activeId}/export`, '_blank');
  };

  const openRefTarget = (ref: any) => {
    if (!ref || typeof ref !== 'object') return;
    if (ref.type === 'case' && ref.id) {
      const kind = ref.case_kind === 'functional' ? 'functional' : 'ui';
      window.location.href = `/testcases/${kind}?case_id=${ref.id}`;
    } else if (ref.type === 'batch' && ref.id) {
      window.location.href = `/reports?batch_id=${ref.id}`;
    } else if (ref.type === 'project' && ref.id) {
      window.location.href = '/projects';
    }
  };

  const uploadFile = async (file: File) => {
    if (activeId == null) return;
    setUploading(true);
    try {
      const form = new FormData();
      form.append('file', file);
      const r = await axios.post(`/api/chat/conversations/${activeId}/attachments`, form);
      setAttachments((prev) => [...prev, { file_ref: r.data.file_ref, filename: r.data.filename }]);
    } catch (e: any) {
      Message.error(e?.response?.data?.detail || '上传失败');
    } finally {
      setUploading(false);
    }
  };

  const importDrafts = async (sid: string, all: boolean) => {
    if (activeId == null) return;
    const ids = selectedDrafts[sid] || [];
    if (!all && ids.length === 0) {
      Message.warning('请先勾选要导入的草稿');
      return;
    }
    try {
      const r = await axios.post(`/api/chat/conversations/${activeId}/import-drafts`, {
        gen_session_id: sid,
        draft_ids: all ? undefined : ids,
      });
      Message.success(`已导入 ${r.data.imported_count} 条用例（跳过 ${r.data.skipped_count} 条重复）`);
      await loadMessages(activeId);
    } catch (e: any) {
      Message.error(e?.response?.data?.detail || '导入失败');
    }
  };

  const regenerate = async (mid: number) => {
    if (activeId == null) return;
    try {
      await axios.post(`/api/chat/conversations/${activeId}/messages/${mid}/regenerate`);
      Message.success('已重新生成');
    } catch (e: any) {
      Message.error(e?.response?.data?.detail || '重新生成失败');
    }
  };

  const applyDiff = async (mid: number, idx: number) => {
    if (activeId == null) return;
    try {
      await axios.post(`/api/chat/conversations/${activeId}/apply-diff`, {
        message_id: mid,
        card_index: idx,
      });
      Message.success('已应用修改（已留痕）');
      await loadMessages(activeId);
    } catch (e: any) {
      if (e?.response?.status === 409) {
        Message.warning('用例已被修改，对照已基于最新版重建，请重新确认');
        await loadMessages(activeId);
      } else {
        Message.error(e?.response?.data?.detail || '应用失败');
      }
    }
  };

  const discardDiff = async (mid: number, idx: number) => {
    if (activeId == null) return;
    try {
      await axios.post(`/api/chat/conversations/${activeId}/discard-diff`, {
        message_id: mid,
        card_index: idx,
      });
      await loadMessages(activeId);
    } catch (e: any) {
      Message.error(e?.response?.data?.detail || '放弃失败');
    }
  };

  const cancelRun = async (batchId: number) => {
    if (activeId == null) return;
    try {
      await axios.post(`/api/chat/conversations/${activeId}/run/${batchId}/cancel`);
      Message.success('已取消执行');
      await loadMessages(activeId);
    } catch (e: any) {
      Message.error(e?.response?.data?.detail || '取消失败');
    }
  };

  const removeConversationRef = async (target: Record<string, unknown>) => {
    if (activeId == null) return;
    const conv = conversations.find((c) => c.id === activeId);
    const remaining = (((conv as any)?.refs as Array<Record<string, unknown>>) || [])
      .filter((r) => !(r.type === target.type && r.id === target.id));
    try {
      const r = await axios.patch(`/api/chat/conversations/${activeId}`, { refs: remaining });
      setConversations((prev) => prev.map((c) => (c.id === activeId ? { ...c, refs: r.data.refs } : c)));
    } catch (e: any) {
      Message.error(e?.response?.data?.detail || '移除引用失败');
    }
  };

  const openRefPicker = async () => {
    setRefPickerVisible(true);
    setRefSearch('');
    await searchRefCases('');
  };

  const searchRefCases = async (q: string) => {
    const conv = conversations.find((c) => c.id === activeId);
    if (!conv?.project_id) {
      Message.warning('对话未绑定项目');
      return;
    }
    try {
      const r = await axios.get('/api/testcases/search', {
        params: { project_id: conv.project_id, q, page: 1, size: 20 },
      });
      setRefOptions(r.data.items || []);
    } catch {
      setRefOptions([]);
    }
  };

  const deleteMessage = async (mid: number) => {
    if (activeId == null) return;
    try {
      await axios.delete(`/api/chat/conversations/${activeId}/messages/${mid}`);
    } catch {
      Message.error('删除失败');
    }
  };

  const renderCard = (card: CardPayload, idx: number, mid: number) => {
    if (card.type === 'draft_set') {
      const status = card.status || 'draft';
      const tag = DRAFT_STATUS_TAG[status] || { color: 'gray', text: status };
      const items = card.items || [];
      const selected = (card.gen_session_id && selectedDrafts[card.gen_session_id]) || [];
      const importable = status === 'draft' || status === 'partial';
      return (
        <div className={styles.card} key={idx}>
          <div className={styles.cardHeader}>
            <div className={styles.cardTitle}>
              草稿集
              <Tag size="small" color={card.case_kind === 'functional' ? 'arcoblue' : 'purple'}>
                {card.case_kind === 'functional' ? '功能用例' : 'UI 用例'}
              </Tag>
              <Tag size="small" color={tag.color}>{tag.text}</Tag>
            </div>
            <Text type="secondary" style={{ fontSize: 12 }}>{items.length} 条</Text>
          </div>
          {status === 'generating' && (
            <Progress percent={card.progress || 0} size="small" style={{ marginBottom: 8 }} />
          )}
          {status === 'failed' && (
            <Text type="error" style={{ fontSize: 12 }}>{card.error || '生成失败'}</Text>
          )}
          {items.map((it) => (
            <div className={styles.draftItem} key={it.draft_id}>
              {importable && (
                <Checkbox
                  checked={selected.includes(it.draft_id)}
                  onChange={(checked) => {
                    if (!card.gen_session_id) return;
                    setSelectedDrafts((prev) => {
                      const cur = prev[card.gen_session_id as string] || [];
                      const next = checked ? [...cur, it.draft_id] : cur.filter((x) => x !== it.draft_id);
                      return { ...prev, [card.gen_session_id as string]: next };
                    });
                  }}
                />
              )}
              <div className={styles.draftTitle}>
                <div>{it.title}</div>
                {(it.steps || []).length > 0 && (
                  <div className={styles.draftSteps}>{(it.steps || []).join(' → ')}</div>
                )}
              </div>
            </div>
          ))}
          {importable && items.length > 0 && (
            <div className={styles.cardFooter}>
              <Button size="small" type="primary" icon={<IconImport />} onClick={() => importDrafts(card.gen_session_id as string, false)}>
                导入选中
              </Button>
              <Button size="small" onClick={() => importDrafts(card.gen_session_id as string, true)}>
                全部导入
              </Button>
              {card.imported_case_ids && card.imported_case_ids.length > 0 && (
                <Text type="secondary" style={{ fontSize: 12 }}>已导入 {card.imported_case_ids.length} 条</Text>
              )}
            </div>
          )}
        </div>
      );
    }
    if (card.type === 'diff') {
      const status = (card.status as string) || 'pending';
      const tagMap: Record<string, { color: string; text: string }> = {
        pending: { color: card.stale ? 'orange' : 'arcoblue', text: card.stale ? '待重新确认' : '待确认' },
        applied: { color: 'green', text: '已应用' },
        discarded: { color: 'gray', text: '已放弃' },
      };
      const tag = tagMap[status] || { color: 'gray', text: status };
      const changes = (card.changes as Array<Record<string, any>>) || [];
      const fieldLabel = (f: string) => ({ description: '步骤描述', expected: '预期结果', target_name: '目标', value: '值' }[f] || f);
      return (
        <div className={styles.card} key={idx}>
          <div className={styles.cardHeader}>
            <div className={styles.cardTitle}>
              修改对照
              <Tag size="small" color="purple">{(card.case_title as string) || `case#${card.case_id}`}</Tag>
              <Tag size="small" color={tag.color}>{tag.text}</Tag>
            </div>
          </div>
          {card.stale && (
            <Text type="warning" style={{ fontSize: 12, display: 'block', marginBottom: 6 }}>
              用例已被修改，对照已基于最新版重建，请重新确认
            </Text>
          )}
          {changes.map((ch, i) => (
            <div className={styles.changeItem} key={i}>
              {ch.kind === 'update' && (
                <span>
                  第 {(ch.step_index as number) + 1} 步 · {fieldLabel(ch.field as string)}：
                  <span className={styles.before}>{String(ch.before ?? '（空）')}</span>
                  {' → '}
                  <span className={styles.after}>{String(ch.after ?? '（空）')}</span>
                </span>
              )}
              {ch.kind === 'add' && (
                <span>
                  新增第 {(ch.step_index as number) + 1} 步：<span className={styles.after}>{String((ch.step as any)?.description || '')}</span>
                </span>
              )}
              {ch.kind === 'remove' && (
                <span>
                  删除第 {(ch.step_index as number) + 1} 步：<span className={styles.before}>{String((ch.step as any)?.description || '')}</span>
                </span>
              )}
            </div>
          ))}
          {status === 'pending' && (
            <div className={styles.cardFooter}>
              <Button size="small" type="primary" onClick={() => applyDiff(mid, idx)}>
                应用修改
              </Button>
              <Button size="small" onClick={() => discardDiff(mid, idx)}>
                放弃
              </Button>
              <Text type="secondary" style={{ fontSize: 12 }}>
                应用后写回用例库并记录变更痕迹
              </Text>
            </div>
          )}
        </div>
      );
    }
    if (card.type === 'run') {
      const status = (card.status as string) || 'running';
      const tagMap: Record<string, { color: string; text: string }> = {
        running: { color: 'blue', text: '执行中' },
        paused: { color: 'orange', text: '已暂停' },
        done: { color: 'green', text: '已完成' },
        failed: { color: 'red', text: '失败' },
        canceled: { color: 'gray', text: '已取消' },
      };
      const tag = tagMap[status] || { color: 'gray', text: status };
      const progress = (card.progress as { total?: number; done?: number }) || {};
      const result = (card.result as { passed?: number; failed?: number } | null) || null;
      const failures = (card.failures as Array<Record<string, any>>) || [];
      const caseTitles = (card.case_titles as string[]) || [];
      return (
        <div className={styles.card} key={idx}>
          <div className={styles.cardHeader}>
            <div className={styles.cardTitle}>
              执行批次 #{card.batch_id as number}
              <Tag size="small" color={card.mode === 'client' ? 'purple' : 'arcoblue'}>
                {card.mode === 'client' ? `客户端 ${card.agent_name || ''}` : '服务端'}
              </Tag>
              <Tag size="small" color={tag.color}>{tag.text}</Tag>
            </div>
            <Text type="secondary" style={{ fontSize: 12 }}>
              {(progress.done ?? 0)}/{progress.total ?? caseTitles.length} 用例
              {result ? ` · 通过 ${result.passed ?? 0} · 失败 ${result.failed ?? 0}` : ''}
            </Text>
          </div>
          {status === 'running' && (
            <Progress
              percent={progress.total ? Math.round(((progress.done ?? 0) / progress.total) * 100) : 0}
              size="small"
              style={{ marginBottom: 8 }}
            />
          )}
          {caseTitles.length > 0 && (
            <div className={styles.draftSteps}>{caseTitles.map((t) => `· ${t}`).join('\n')}</div>
          )}
          {failures.length > 0 && (
            <div style={{ marginTop: 6 }}>
              {failures.map((f, i) => (
                <div className={styles.changeItem} key={i}>
                  <Text style={{ fontSize: 13 }}>{String(f.case_title || '')}</Text>
                  <div className={styles.draftSteps}>
                    失败步骤：{String(f.step || '（未记录）')}
                    {(f.console as string[])?.length ? `\n${(f.console as string[]).join('\n')}` : ''}
                  </div>
                  {f.screenshot_url ? (
                    <a href={String(f.screenshot_url)} target="_blank" rel="noreferrer" style={{ fontSize: 12 }}>
                      查看失败截图
                    </a>
                  ) : null}
                </div>
              ))}
            </div>
          )}
          <div className={styles.cardFooter}>
            <a href={`/reports?batch_id=${card.batch_id}`}>
              <Button size="small" type="primary">
                查看报告
              </Button>
            </a>
            {(status === 'running' || status === 'paused') && (
              <Button size="small" onClick={() => cancelRun(card.batch_id as number)}>
                取消执行
              </Button>
            )}
          </div>
        </div>
      );
    }
    // 后续故事（证据）卡片在此扩展
    if (card.type === 'evidence') {
      const status = (card.status as string) || 'ready';
      const tag = status === 'insufficient'
        ? { color: 'gray', text: '证据不足' }
        : { color: 'arcoblue', text: '已归因' };
      const suggestions = (card.suggestions as Array<Record<string, any>>) || [];
      const cases = (((card.evidence as Record<string, any>) || {}).cases as Array<Record<string, any>>) || [];
      const grounded = (card.grounded_on as string[]) || [];
      return (
        <div className={styles.card} key={idx}>
          <div className={styles.cardHeader}>
            <div className={styles.cardTitle}>
              失败归因
              <Tag size="small" color="purple">批次 #{card.batch_id as number}</Tag>
              <Tag size="small" color={tag.color}>{tag.text}</Tag>
            </div>
          </div>
          <div style={{ fontSize: 13, whiteSpace: 'pre-wrap' }}>{String(card.analysis || '')}</div>
          {grounded.length > 0 && (
            <div className={styles.draftSteps}>依据：{grounded.join('；')}</div>
          )}
          {cases.map((c, i) => (
            <div className={styles.changeItem} key={i}>
              <div style={{ fontSize: 12 }}>
                {String(c.case_title || '')} · 失败步骤：{String(c.step || '—')}
              </div>
              {((c.console as string[]) || []).length > 0 && (
                <div className={styles.draftSteps}>{((c.console as string[]) || []).slice(0, 3).join('\n')}</div>
              )}
              {c.screenshot_url ? (
                <a href={String(c.screenshot_url)} target="_blank" rel="noreferrer" style={{ fontSize: 12 }}>
                  查看失败截图
                </a>
              ) : null}
            </div>
          ))}
          {suggestions.length > 0 && (
            <div style={{ marginTop: 8 }}>
              {suggestions.map((s, i) => (
                <div className={styles.changeItem} key={i}>
                  <div style={{ fontSize: 13 }}>💡 {String(s.title || '')}</div>
                  {s.reason ? <div className={styles.draftSteps}>{String(s.reason)}</div> : null}
                  <Button
                    size="mini"
                    type="primary"
                    style={{ marginTop: 4 }}
                    onClick={() => sendPrompt(String(s.apply_prompt || ''))}
                  >
                    按此修改
                  </Button>
                </div>
              ))}
            </div>
          )}
        </div>
      );
    }
    return (
      <div className={styles.card} key={idx}>
        <Text type="secondary" style={{ fontSize: 12 }}>
          卡片类型 {card.type}（该能力将在后续版本开放）
        </Text>
      </div>
    );
  };

  const activeConv = conversations.find((c) => c.id === activeId);

  if (!chatEnabled) {
    return (
      <div className={styles.page} style={{ alignItems: 'center', justifyContent: 'center' }}>
        <Empty description="对话功能未启用（chat_enabled=false）" />
      </div>
    );
  }

  return (
    <div className={styles.page}>
      <div className={styles.sidebar}>
        <div className={styles.sidebarHeader}>
          <Button type="primary" long icon={<IconPlus />} onClick={() => setCreateVisible(true)}>
            新建对话
          </Button>
          {activeId != null && (
            <Button long size="small" style={{ marginTop: 6 }} icon={<IconDownload />} onClick={exportConversation}>
              导出对话
            </Button>
          )}
        </div>
        <div className={styles.convList}>
          {conversations.map((c) => (
            <div
              key={c.id}
              className={`${styles.convItem} ${c.id === activeId ? styles.convItemActive : ''}`}
              onClick={() => setActiveId(c.id)}
            >
              <div style={{ minWidth: 0, flex: 1 }}>
                <div className={styles.convTitle}>{c.title}</div>
                <div className={styles.convMeta}>{c.updated_at ? new Date(c.updated_at).toLocaleString() : ''}</div>
              </div>
              <Popconfirm title="删除该对话？" onOk={() => deleteConversation(c.id)}>
                <IconDelete style={{ fontSize: 14, color: '#86909c' }} onClick={(e) => e.stopPropagation()} />
              </Popconfirm>
            </div>
          ))}
          {conversations.length === 0 && (
            <Empty description="暂无对话" style={{ marginTop: 40 }} />
          )}
        </div>
      </div>

      <div className={styles.main}>
        {activeId == null ? (
          <div className={styles.emptyWrap}>
            <Empty description="选择或新建一个对话，开始用自然语言完成用例生成、修改与执行" />
          </div>
        ) : (
          <>
            <div className={styles.messages}>
              {loadingMsgs && <Spin style={{ display: 'block', margin: '12px auto' }} />}
              {messages.map((m) => (
                <div className={`${styles.msgRow} ${m.role === 'user' ? styles.user : ''}`} key={m.id}>
                  <div style={{ maxWidth: '76%' }}>
                    <div className={`${styles.bubble} ${m.role === 'user' ? styles.userBubble : styles.assistantBubble}`}>
                      {m.content}
                    </div>
                    {m.role === 'user' && (m.refs || []).filter((r: any) => r && r.type !== 'file').length > 0 && (
                      <div className={styles.msgTools} style={{ justifyContent: 'flex-end' }}>
                        {(m.refs || [])
                          .filter((r: any) => r && r.type !== 'file')
                          .map((r: any, i: number) => (
                            <Tag
                              key={i}
                              size="small"
                              color="arcoblue"
                              style={{ cursor: 'pointer' }}
                              onClick={() => openRefTarget(r)}
                            >
                              引用：{r.label || `${r.type}#${r.id}`}
                            </Tag>
                          ))}
                      </div>
                    )}
                    {(m.cards || []).map((c, i) => renderCard(c, i, m.id))}
                    {m.role === 'assistant' && (
                      <div className={styles.msgTools}>
                        <span className={styles.msgTool} onClick={() => regenerate(m.id)}>
                          <IconRefresh style={{ marginRight: 2 }} />重新生成
                        </span>
                        <Popconfirm title="删除这条消息？" onOk={() => deleteMessage(m.id)}>
                          <span className={styles.msgTool}>
                            <IconDelete style={{ marginRight: 2 }} />删除
                          </span>
                        </Popconfirm>
                      </div>
                    )}
                  </div>
                </div>
              ))}
              <div ref={bottomRef} />
            </div>

            <div className={styles.inputArea}>
              {((activeConv?.refs as Array<Record<string, unknown>>) || []).length > 0 && (
                <div className={styles.attachRow}>
                  {((activeConv?.refs as Array<Record<string, unknown>>) || []).map((r, i) => (
                    <Tag
                      key={`conv-ref-${i}`}
                      color="arcoblue"
                      closable
                      onClose={() => removeConversationRef(r)}
                      style={{ cursor: 'pointer' }}
                    >
                      <span onClick={() => openRefTarget(r)}>
                        引用：{String(r.label || `${r.type}#${r.id}`)}
                      </span>
                    </Tag>
                  ))}
                </div>
              )}
              {(attachments.length > 0 || refCases.length > 0) && (
                <div className={styles.attachRow}>
                  {refCases.map((c, i) => (
                    <Tag
                      key={`ref-${c.id}`}
                      color="arcoblue"
                      closable
                      onClose={() => setRefCases((prev) => prev.filter((_, j) => j !== i))}
                    >
                      引用：{c.name}
                    </Tag>
                  ))}
                  {attachments.map((a, i) => (
                    <Tag key={i} closable onClose={() => setAttachments((prev) => prev.filter((_, j) => j !== i))}>
                      {a.filename}
                    </Tag>
                  ))}
                </div>
              )}
              <div className={styles.inputRow}>
                <Input.TextArea
                  value={input}
                  onChange={setInput}
                  autoSize={{ minRows: 1, maxRows: 5 }}
                  placeholder="描述需求生成用例、或让我修改/执行用例…（Enter 发送，Shift+Enter 换行）"
                  onKeyDown={(e) => {
                    if (e.key === 'Enter' && !e.shiftKey) {
                      e.preventDefault();
                      if (!sending) send();
                    }
                  }}
                  style={{ flex: 1 }}
                />
                <Button icon={<IconList />} onClick={openRefPicker}>
                  引用用例
                </Button>
                <Button
                  icon={<IconUpload />}
                  loading={uploading}
                  onClick={() => fileInputRef.current?.click()}
                />
                <input
                  ref={fileInputRef}
                  type="file"
                  style={{ display: 'none' }}
                  accept=".md,.txt,.docx,.pdf,.doc,.png,.jpg,.jpeg"
                  onChange={(e) => {
                    const f = e.target.files?.[0];
                    if (f) uploadFile(f);
                    e.target.value = '';
                  }}
                />
                <Button type="primary" icon={<IconSend />} loading={sending} onClick={send}>
                  发送
                </Button>
              </div>
              <div className={styles.hint}>
                当前项目：{activeConv?.project_id ? (projects.find((p) => p.id === activeConv.project_id)?.name || activeConv.project_id) : '未绑定'} ·
                对话仅对本人可见，动作受项目权限约束
              </div>
            </div>
          </>
        )}
      </div>

      <Modal
        title="新建对话"
        visible={createVisible}
        onOk={createConversation}
        onCancel={() => setCreateVisible(false)}
        okText="创建"
      >
        <div style={{ marginBottom: 8 }}>选择对话绑定的项目：</div>
        <Select
          placeholder="选择项目"
          style={{ width: '100%' }}
          value={createProjectId}
          onChange={setCreateProjectId}
          showSearch
        >
          {projects.map((p) => (
            <Select.Option key={p.id} value={p.id}>
              {p.name}
            </Select.Option>
          ))}
        </Select>
      </Modal>

      <Modal
        title="引用用例"
        visible={refPickerVisible}
        onCancel={() => setRefPickerVisible(false)}
        footer={null}
      >
        <Input.Search
          placeholder="按名称搜索用例"
          value={refSearch}
          onChange={setRefSearch}
          onSearch={(v) => searchRefCases(v)}
          style={{ marginBottom: 8 }}
        />
        <div style={{ maxHeight: 320, overflowY: 'auto' }}>
          {refOptions.map((c) => (
            <div
              key={c.id}
              className={styles.convItem}
              onClick={() => {
                setRefCases((prev) =>
                  prev.some((x) => x.id === c.id)
                    ? prev
                    : [...prev, { id: c.id, name: c.name, case_kind: (c as any).case_kind }],
                );
                setRefPickerVisible(false);
              }}
            >
              <div className={styles.convTitle}>
                #{c.id} {c.name}
              </div>
            </div>
          ))}
          {refOptions.length === 0 && <Empty description="暂无用例" style={{ marginTop: 20 }} />}
        </div>
      </Modal>
    </div>
  );
};

export default ChatPage;
