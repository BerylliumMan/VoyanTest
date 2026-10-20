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
  Radio,
  Select,
  Spin,
  Tag,
  Typography,
} from '@arco-design/web-react';
import {
  IconCaretDown,
  IconCaretRight,
  IconDelete,
  IconDownload,
  IconEdit,
  IconImport,
  IconList,
  IconLoading,
  IconPlus,
  IconRefresh,
  IconRobot,
  IconSend,
  IconStop,
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
  source?: string;
  refs?: Array<Record<string, unknown>>;
  case_kind?: string | null;
  environment_id?: number | null;
  agent_id?: number | null;
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
  thinking?: string | null;
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
  cancelled: { color: 'gray', text: '已停止' },
};

// 失败截图链接：统一为站点绝对路径（相对路径在不同路由深度下解析位置不一致）
const screenshotHref = (raw: unknown): string => {
  const s = String(raw || '').trim();
  if (!s) return '';
  if (/^https?:\/\//i.test(s)) return s;
  return '/' + s.replace(/^\/+/, '');
};

// ZCode 式「已工作」计时格式（导出供测试）
export const fmtWorked = (sec: number): string => {
  const total = Math.max(0, Math.round(sec));
  if (total < 60) return `${total} 秒`;
  const m = Math.floor(total / 60);
  const s = total % 60;
  return `${m} 分 ${s} 秒`;
};

// 思考块（默认折叠，ZCode 式「思考 · 持续 N 秒」）；running 时流式展示且展开跟随
const ThinkingItem: React.FC<{
  text: string;
  duration?: number | null;
  running?: boolean;
}> = ({ text, duration, running }) => {
  const [open, setOpen] = useState(false);
  return (
    <div className={styles.tlThinking}>
      <div className={styles.tlThinkingHead} onClick={() => setOpen((v) => !v)}>
        {open ? <IconCaretDown /> : <IconCaretRight />}
        <span className={styles.tlThinkingLabel}>
          思考{running ? '中' : ''}
          {!running && duration != null ? ` · 持续 ${duration} 秒` : ''}
        </span>
        {running ? <IconLoading className={styles.tlSpin} /> : null}
      </div>
      {open && text ? <div className={styles.tlThinkingBody}>{text}</div> : null}
    </div>
  );
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
  // 流式输出：thinking/reply 增量；targetId 为被「重新生成」的消息 id（null = 新一轮回复）
  const [stream, setStream] = useState<{ targetId: number | null; thinking: string; reply: string } | null>(null);
  const [streamOpen, setStreamOpen] = useState(true);
  // 035：工具调用实时行（tool.call.start/end 事件；终稿落库后清空）
  const [liveTools, setLiveTools] = useState<Array<{
    callId: string;
    tool: string;
    argsDigest: string;
    status: string;
    summary?: string;
    durationMs?: number;
    truncated?: boolean;
    excerpt?: string;
  }>>([]);
  // 036：工具行输出节选展开（call_id 集合）
  const [toolOpen, setToolOpen] = useState<Record<string, boolean>>({});
  // 探索时间线瞬时流：browse_id → 当前思考增量（done 后清空，由卡片时间线接管）
  const [browseLive, setBrowseLive] = useState<Record<string, string>>({});
  // 编辑已发送消息（保存并从该条重新生成）
  const [editingId, setEditingId] = useState<number | null>(null);
  const [editText, setEditText] = useState('');
  const streamReplyStartedRef = useRef(false);
  // 执行上下文：项目环境列表 / 客户端 Agent 列表
  const [envs, setEnvs] = useState<Array<{ id: number; name: string; is_default?: boolean }>>([]);
  const [agents, setAgents] = useState<Array<{ id: number; name: string; status?: string }>>([]);
  const [pcClient, setPcClient] = useState<{ agent_id?: number | null; agent_name?: string | null } | null>(null);
  const [refOptions, setRefOptions] = useState<Array<{ id: number; name: string }>>([]);
  const bottomRef = useRef<HTMLDivElement | null>(null);
  const msgListRef = useRef<HTMLDivElement | null>(null);
  // 滚动只作用于消息容器自身：scrollIntoView 会连带滚动外层容器，把顶部
  // 执行上下文栏（项目/用例类型/环境）滚出可视区（2026-10-14 实测修复）
  const scrollMsgListToBottom = useCallback((smooth: boolean) => {
    const el = msgListRef.current;
    if (!el) return;
    el.scrollTo({ top: el.scrollHeight, behavior: smooth ? 'smooth' : 'auto' });
  }, []);
  const wsRef = useRef<WebSocket | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  const [convFilter, setConvFilter] = useState<'all' | 'user' | 'external'>('all');

  const loadConversations = useCallback(async () => {
    try {
      const params = convFilter === 'all' ? {} : { source: convFilter };
      const r = await axios.get('/api/chat/conversations', { params });
      setConversations(r.data.items || []);
      return r.data.items as Conversation[];
    } catch {
      return [];
    }
  }, [convFilter]);

  const loadMessages = useCallback(async (cid: number) => {
    setLoadingMsgs(true);
    try {
      const r = await axios.get(`/api/chat/conversations/${cid}/messages`, { params: { limit: 200 } });
      setMessages(r.data.items || []);
    } finally {
      setLoadingMsgs(false);
    }
  }, []);

  // 更新对话执行上下文（项目/类型/环境/客户端）；-1 表示清空（默认环境/服务端）
  const patchConversation = useCallback(async (fields: Record<string, unknown>) => {
    if (activeId == null) return;
    try {
      const r = await axios.patch(`/api/chat/conversations/${activeId}`, fields);
      const updated = r.data as Conversation;
      setConversations((prev) => prev.map((c) => (c.id === updated.id ? { ...c, ...updated } : c)));
      if (fields.project_id != null) {
        Message.success('项目已切换');
      }
    } catch (e: any) {
      Message.error(e?.response?.data?.detail || '设置更新失败');
    }
  }, [activeId]);

  // 默认选中「当前 PC 的客户端」：会话未设置过（agent_id 为 null）且本机客户端在线时自动绑定，
  // 之后执行用例默认走该客户端（显式选「服务端」存 0，不再被自动接管）。
  useEffect(() => {
    const conv = conversations.find((c) => c.id === activeId);
    if (!conv) return;
    if (conv.agent_id !== null && conv.agent_id !== undefined) return;
    const name = pcClient?.agent_name;
    if (!name) return;
    const target = agents.find((a) => a.name === name);
    if (target && target.id && target.status === 'online') {
      patchConversation({ agent_id: target.id });
    }
  }, [activeId, conversations, agents, pcClient, patchConversation]);

  // 环境列表：跟随当前对话项目
  const activeConvForEnvs = conversations.find((c) => c.id === activeId);
  const activeProjectId = activeConvForEnvs?.project_id ?? null;
  useEffect(() => {
    if (activeProjectId == null) {
      setEnvs([]);
      return;
    }
    axios.get(`/api/projects/${activeProjectId}/environments`)
      .then((r) => setEnvs(r.data || []))
      .catch(() => setEnvs([]));
  }, [activeProjectId]);

  // 客户端 Agent 列表（含在线状态）
  useEffect(() => {
    axios.get('/api/agents')
      .then((r) => setAgents(Array.isArray(r.data) ? r.data : []))
      .catch(() => setAgents([]));
  }, []);

  // 当前 PC 的客户端（按请求来源 IP 与在线客户端匹配；用于默认选中与标注）
  useEffect(() => {
    axios.get('/api/chat/current-client')
      .then((r) => setPcClient(r.data || null))
      .catch(() => setPcClient(null));
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
    setStream(null);
  }, [activeId, loadMessages]);

  // 流式：reply 开始输出时自动折叠「思考」；流式期间保持滚动到底部
  useEffect(() => {
    if (!stream) {
      streamReplyStartedRef.current = false;
      return;
    }
    if (stream.reply && !streamReplyStartedRef.current) {
      streamReplyStartedRef.current = true;
      setStreamOpen(false);
    }
    scrollMsgListToBottom(false);
  }, [stream]);

  // 静默刷新（不触发加载态）：运行中卡片的 WS 兜底对账
  const refreshMessagesQuiet = useCallback(async (cid: number) => {
    try {
      const r = await axios.get(`/api/chat/conversations/${cid}/messages`, { params: { limit: 200 } });
      setMessages(r.data.items || []);
    } catch {
      /* ignore */
    }
  }, []);

  // 追加消息（按 id 去重）：发送接口响应往往晚于 WS message.created 事件，
  // 直接 append 会把同一条消息渲染两遍（发送后不刷新出现两条的缺陷，2026-10-10 修复）
  const appendMessages = useCallback((incoming: Array<ChatMessage | null | undefined>) => {
    const items = (incoming || []).filter(Boolean) as ChatMessage[];
    if (!items.length) return;
    setMessages((prev) => {
      const seen = new Set(prev.map((m) => m.id));
      const add = items.filter((m) => !seen.has(m.id));
      return add.length ? [...prev, ...add] : prev;
    });
  }, []);

  // 存在「运行中/生成中」卡片时，每 5s 静默对账一次（WS 断线/丢事件时卡片仍会收敛）
  useEffect(() => {
    if (activeId == null) return undefined;
    const hasActive = messages.some((m) => (m.cards || []).some((c) => {
      const t = String(c.type || '');
      const s = String(c.status || '');
      return (t === 'run' && (s === 'running' || s === 'paused'))
        || (t === 'draft_set' && (s === 'generating' || s === 'queued'))
        || (t === 'browse' && s === 'running');
    }));
    if (!hasActive) return undefined;
    const timer = window.setInterval(() => { refreshMessagesQuiet(activeId); }, 5000);
    return () => window.clearInterval(timer);
  }, [activeId, messages, refreshMessagesQuiet]);

  // 探索卡「已工作」计数：本地每秒走秒（ZCode 式连续计时，不依赖网络事件）
  const [, setClockTick] = useState(0);
  const hasRunningBrowse = messages.some((m) =>
    (m.cards || []).some((c) => String(c.type) === 'browse' && String(c.status) === 'running'));
  useEffect(() => {
    if (!hasRunningBrowse) return undefined;
    const timer = window.setInterval(() => setClockTick((v) => v + 1), 1000);
    return () => window.clearInterval(timer);
  }, [hasRunningBrowse]);

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
          if (msg.role === 'assistant') {
            setStream(null); // 终稿落库 → 结束流式预览
            setLiveTools([]);
          }
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
        } else if (data.type === 'message.stream_start') {
          streamReplyStartedRef.current = false;
          setLiveTools([]);
          setStream({ targetId: (data.target_message_id as number) ?? null, thinking: '', reply: '' });
          setStreamOpen(true);
        } else if (data.type === 'turn.stopped') {
          // 停止按钮（本端或他端）→ 清掉流式预览，发送按钮由 send() 的 finally 收敛
          setStream(null);
          setLiveTools([]);
        } else if (data.type === 'tool.call.start') {
          setLiveTools((prev) => [...prev, {
            callId: String(data.call_id || ''),
            tool: String(data.tool || ''),
            argsDigest: String(data.args_digest || ''),
            status: 'running',
          }]);
        } else if (data.type === 'tool.call.end') {
          const cid = String(data.call_id || '');
          setLiveTools((prev) => prev.map((t) => (t.callId === cid
            ? {
                ...t,
                status: data.ok ? 'ok' : 'failed',
                summary: String(data.summary || ''),
                durationMs: Number(data.duration_ms || 0),
                truncated: Boolean(data.truncated),
                excerpt: typeof data.excerpt === 'string' ? data.excerpt : t.excerpt,
              }
            : t)));
        } else if (data.type === 'message.stream') {
          const channel = String(data.channel || '');
          const delta = String(data.delta || '');
          if (!delta) return;
          setStream((prev) => {
            if (!prev) {
              return {
                targetId: (data.target_message_id as number) ?? null,
                thinking: channel === 'thinking' ? delta : '',
                reply: channel === 'reply' ? delta : '',
              };
            }
            return channel === 'thinking'
              ? { ...prev, thinking: prev.thinking + delta }
              : { ...prev, reply: prev.reply + delta };
          });
        } else if (data.type === 'browse.stream') {
          // ZCode 式时间线：探索引擎思考增量（瞬时，不落库；done 后由卡片时间线接管）
          const bid = String(data.browse_id || '');
          if (!bid) return;
          if (data.done) {
            setBrowseLive((prev) => {
              if (!(bid in prev)) return prev;
              const next = { ...prev };
              delete next[bid];
              return next;
            });
          } else {
            const delta = String(data.delta || '');
            if (delta) setBrowseLive((prev) => ({ ...prev, [bid]: (prev[bid] || '') + delta }));
          }
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
    scrollMsgListToBottom(true);
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
    // 立即清空输入框（POST 会阻塞到本轮回复完成，等响应再清会让用户一直看到旧内容）
    const draft = { text: input, attachments, refs: refCases };
    setSending(true);
    setInput('');
    setAttachments([]);
    setRefCases([]);
    try {
      const r = await axios.post(`/api/chat/conversations/${activeId}/messages`, {
        content: text,
        refs: draft.refs.length
          ? draft.refs.map((c) => ({ type: 'case', id: c.id, label: c.name, case_kind: (c as any).case_kind || 'ui' }))
          : undefined,
        attachments: draft.attachments.length ? draft.attachments : undefined,
      });
      const { user_message, assistant_message, stopped } = r.data;
      appendMessages([user_message, assistant_message]);
      if (stopped) Message.info('已停止本轮回复');
    } catch (e: any) {
      // 失败（如上一轮仍在处理中 409）时恢复草稿，避免用户输入丢失
      setInput(draft.text);
      setAttachments(draft.attachments);
      setRefCases(draft.refs);
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
      appendMessages([r.data.user_message, r.data.assistant_message]);
      if (r.data.stopped) Message.info('已停止本轮回复');
    } catch (e: any) {
      Message.error(e?.response?.data?.detail || '发送失败');
    } finally {
      setSending(false);
    }
  };

  // 停止：取消当前正在生成的这轮回复（发送按钮在生成期间显示为「停止」）
  const stopTurn = async () => {
    if (activeId == null) return;
    try {
      const r = await axios.post(`/api/chat/conversations/${activeId}/stop`, { kind: 'turn' });
      if (r.data?.total) Message.info('已停止生成');
    } catch {
      /* ignore：send() 的响应会收敛状态 */
    }
    setStream(null);
  };

  // 编辑已发送的用户消息 → 保存并从该条重新生成（其后消息会被清除）
  const saveEdit = async (mid: number) => {
    if (activeId == null) return;
    const text = editText.trim();
    if (!text) {
      Message.warning('消息内容不能为空');
      return;
    }
    setSending(true);
    try {
      const r = await axios.patch(`/api/chat/conversations/${activeId}/messages/${mid}`, {
        content: text,
        regenerate: true,
      });
      // 本地截断 + 更新（WS 的 message.deleted/updated 已先行，幂等兜底）
      setMessages((prev) => prev
        .filter((x) => x.id <= mid)
        .map((x) => (x.id === mid ? { ...x, content: text } : x)));
      appendMessages([r.data.user_message, r.data.assistant_message]);
      setEditingId(null);
      if (r.data.stopped) Message.info('已停止本轮回复');
    } catch (e: any) {
      Message.error(e?.response?.data?.detail || '保存失败');
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
      const r = await axios.post(`/api/chat/conversations/${activeId}/messages/${mid}/regenerate`);
      if (r.data?.stopped) Message.info('已停止本轮回复');
      else Message.success('已重新生成');
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
        params: {
          project_id: conv.project_id,
          q,
          page: 1,
          size: 20,
          case_kind:
            conv.case_kind === 'api' ? 'api' : conv.case_kind === 'functional' ? 'functional' : 'ui',
        },
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

  // 停止进行中的任务（浏览器探索 / 用例生成）
  const stopTask = async (kind: 'browse' | 'generate', key?: string | null) => {
    if (activeId == null) return;
    try {
      const r = await axios.post(`/api/chat/conversations/${activeId}/stop`, {
        kind,
        key: key || undefined,
      });
      if ((r.data?.total || 0) > 0) Message.success('已停止');
      else Message.info('没有进行中的任务');
      await refreshMessagesQuiet(activeId);
    } catch (e: any) {
      Message.error(e?.response?.data?.detail || '停止失败');
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
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 8 }}>
              <Progress percent={card.progress || 0} size="small" style={{ flex: 1, marginBottom: 0 }} />
              <Button size="small" onClick={() => stopTask('generate', card.gen_session_id as string)}>
                停止
              </Button>
            </div>
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
    if (card.type === 'browse') {
      const status = String(card.status || 'running');
      const tagMap: Record<string, { color: string; text: string }> = {
        running: { color: 'arcoblue', text: '探索中' },
        done: { color: 'green', text: '已完成' },
        failed: { color: 'red', text: '失败' },
        stopped: { color: 'gray', text: '已停止' },
      };
      const tag = tagMap[status] || { color: 'gray', text: status };
      const pages = (card.pages as Array<Record<string, any>>) || [];
      const features = (card.features as string[]) || [];
      const steps = (card.steps as Array<Record<string, any>>) || [];
      const timeline = (card.timeline as Array<Record<string, any>>) || [];
      const gen = (card.generation as Record<string, any>) || null;
      const bid = String(card.browse_id || '');
      const live = browseLive[bid] || '';
      // 运行中且无警告时按 started_at 本地走秒（WS/轮询之外也有连续计数）
      let workedSec: number | null = null;
      if (status === 'running' && card.started_at) {
        const t0 = Date.parse(String(card.started_at));
        if (!Number.isNaN(t0)) workedSec = (Date.now() - t0) / 1000;
      } else if (card.elapsed != null) {
        workedSec = Number(card.elapsed) || 0;
      }
      return (
        <div className={styles.card} key={idx}>
          <div className={styles.cardHeader}>
            <div className={styles.cardTitle}>
              浏览器探索
              <Tag size="small" color={tag.color}>
                {status === 'running' && <IconLoading className={styles.iconMarginRightSmall} />}
                {tag.text}
              </Tag>
              {workedSec != null ? (
                <Text type="secondary" style={{ fontSize: 12 }}>
                  {status === 'running' ? `已工作 ${fmtWorked(workedSec)}` : `用时 ${fmtWorked(workedSec)}`}
                </Text>
              ) : null}
            </div>
          </div>
          <div className={styles.browseGoal}>
            <Text type="secondary" style={{ fontSize: 12 }}>🌐 {card.url as string}</Text>
            <div>
              {card.goal as string}
              {card.has_credentials ? <Text type="secondary" style={{ fontSize: 12 }}>（使用提供的账号登录）</Text> : null}
            </div>
            <Text type="secondary" style={{ fontSize: 12 }}>
              {card.browser_mode === 'client'
                ? `浏览器：客户端「${(card.agent_name as string) || ''}」（本机可见）`
                : '浏览器：服务端无头'}
            </Text>
          </div>
          {(timeline.length > 0 || live || status === 'running') && (
            <div className={styles.browseTimeline}>
              {timeline.length > 0
                ? timeline.map((it, i) =>
                    it.type === 'thinking' ? (
                      <ThinkingItem
                        key={i}
                        text={String(it.text || '')}
                        duration={it.duration != null ? Number(it.duration) : null}
                      />
                    ) : it.type === 'text' ? (
                      <div key={i} className={styles.tlText}>{String(it.text || '')}</div>
                    ) : (
                      <div key={i} className={styles.tlStep}>
                        <span className={styles.tlStepMark}>{it.ok === false ? '✗' : '·'}</span>
                        <span className={styles.tlStepLabel}>{String(it.label || '')}</span>
                        {it.ok === false ? (
                          <Tag size="small" color="red">失败</Tag>
                        ) : (
                          <Tag size="small" color="green">已完成</Tag>
                        )}
                      </div>
                    ),
                  )
                : // 旧卡片无 timeline：退回 steps 渲染（后向兼容）
                  steps.slice(-8).map((s, i) => (
                    <div key={i} className={styles.tlStep}>
                      <span className={styles.tlStepMark}>{s.ok === false ? '✗' : '·'}</span>
                      <span className={styles.tlStepLabel}>{s.label as string}</span>
                    </div>
                  ))}
              {status === 'running' && live ? <ThinkingItem text={live} running /> : null}
              {status === 'running' && !live && timeline.length === 0 && steps.length === 0 ? (
                <Text type="secondary" style={{ fontSize: 12 }}>正在连接浏览器…</Text>
              ) : null}
            </div>
          )}
          {status === 'running' && (
            <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
              <Text type="secondary" style={{ fontSize: 12 }}>探索在后台进行，完成后会自动基于观察结果生成用例草稿</Text>
              <Button size="small" onClick={() => stopTask('browse', card.browse_id as string)}>
                停止探索
              </Button>
            </div>
          )}
          {card.summary ? <div className={styles.browseSummary}>{card.summary as string}</div> : null}
          {features.length > 0 && (
            <div className={styles.browseFeatures}>
              <Text type="secondary" style={{ fontSize: 12 }}>主要功能</Text>
              {features.slice(0, 10).map((f, i) => (
                <div key={i}>· {f}</div>
              ))}
            </div>
          )}
          {((card.network as string[]) || []).length > 0 && (
            <div className={styles.browseFeatures}>
              <Text type="secondary" style={{ fontSize: 12 }}>网络异常</Text>
              {((card.network as string[]) || []).slice(0, 5).map((n, i) => (
                <div key={i} style={{ color: '#d4380d', fontSize: 12 }}>· {n}</div>
              ))}
            </div>
          )}
          {pages.length > 0 && (
            <div className={styles.browsePages}>
              {pages.map((p, i) => (
                <a key={i} href={(p.url as string) || '#'} target="_blank" rel="noreferrer" className={styles.browsePage}>
                  {p.screenshot ? (
                    <img src={`/${p.screenshot as string}`} alt={(p.title as string) || ''} />
                  ) : (
                    <div className={styles.browsePageNoShot}>无截图</div>
                  )}
                  <span>{(p.title as string) || (p.url as string)}</span>
                </a>
              ))}
            </div>
          )}
          {status !== 'running' && gen && (
            <div className={styles.cardFooter}>
              {gen.started ? (
                <Text type="secondary" style={{ fontSize: 12 }}>已发起用例生成，见下方草稿卡</Text>
              ) : (
                <Text type="secondary" style={{ fontSize: 12 }}>
                  未发起生成{gen.error ? `：${String(gen.error)}` : '（未采集到页面）'}
                </Text>
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
      const envName = (card.environment_name as string) || '';
      const envUrl = (card.environment_url as string) || '';
      const envWarning = (card.env_warning as string) || '';
      const elapsed = typeof card.elapsed_s === 'number' ? (card.elapsed_s as number) : undefined;
      const current = (card.current as string) || '';
      return (
        <div className={styles.card} key={idx}>
          <div className={styles.cardHeader}>
            <div className={styles.cardTitle}>
              执行批次 #{card.batch_id as number}
              <Tag size="small" color={card.mode === 'client' ? 'purple' : 'arcoblue'}>
                {card.mode === 'client' ? `客户端 ${card.agent_name || ''}` : '服务端'}
              </Tag>
              {card.suite_name ? (
                <Tag size="small" color="cyan">用例集 {card.suite_name as string}</Tag>
              ) : null}
              {envName ? (
                <span title={envUrl}>
                  <Tag size="small" color="gray">环境 {envName}</Tag>
                </span>
              ) : null}
              <Tag size="small" color={tag.color}>{tag.text}</Tag>
            </div>
            <Text type="secondary" style={{ fontSize: 12 }}>
              {(progress.done ?? 0)}/{progress.total ?? caseTitles.length} 用例
              {status === 'running' && elapsed != null ? ` · 已用 ${elapsed}s` : ''}
              {result ? ` · 通过 ${result.passed ?? 0} · 失败 ${result.failed ?? 0}` : ''}
            </Text>
          </div>
          {envWarning ? (
            <div style={{ fontSize: 12, color: '#d97706', marginBottom: 6, lineHeight: 1.6 }}>
              ⚠ {envWarning}
            </div>
          ) : null}
          {status === 'running' && (
            <Progress
              percent={progress.total ? Math.round(((progress.done ?? 0) / progress.total) * 100) : 0}
              size="small"
              style={{ marginBottom: 8 }}
            />
          )}
          {status === 'running' && current ? (
            <div className={styles.draftSteps} style={{ marginBottom: 6 }}>当前：{current}</div>
          ) : null}
          {caseTitles.length > 0 && (
            <div className={styles.draftSteps}>{caseTitles.map((t) => `· ${t}`).join('\n')}</div>
          )}
          {failures.length > 0 && (
            <div style={{ marginTop: 6 }}>
              {failures.map((f, i) => (
                <div className={styles.changeItem} key={i}>
                  <Text style={{ fontSize: 13 }}>{String(f.case_title || '')}</Text>
                  <div className={styles.draftSteps}>
                    失败步骤：{String(f.step || '（未记录）').slice(0, 500)}
                    {(f.console as string[])?.length ? `\n${(f.console as string[]).join('\n')}` : ''}
                  </div>
                  {f.screenshot_url ? (
                    <a href={screenshotHref(f.screenshot_url)} target="_blank" rel="noreferrer" style={{ fontSize: 12 }}>
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
                <a href={screenshotHref(c.screenshot_url)} target="_blank" rel="noreferrer" style={{ fontSize: 12 }}>
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
          <div style={{ padding: '0 4px 8px' }}>
            <Select
              size="small"
              value={convFilter}
              onChange={(v) => setConvFilter(v as 'all' | 'user' | 'external')}
              style={{ width: '100%' }}
              options={[
                { label: '全部对话', value: 'all' },
                { label: '我的对话', value: 'user' },
                { label: '外部接入', value: 'external' },
              ]}
            />
          </div>
          {conversations.map((c) => (
            <div
              key={c.id}
              className={`${styles.convItem} ${c.id === activeId ? styles.convItemActive : ''}`}
              onClick={() => setActiveId(c.id)}
            >
              <div style={{ minWidth: 0, flex: 1 }}>
                <div className={styles.convTitle}>
                  {c.source === 'external' && (
                    <Tag size="small" color="arcoblue" style={{ marginRight: 4 }}>
                      外部
                    </Tag>
                  )}
                  {c.title}
                </div>
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
            {/* 执行上下文选择：项目 → 用例类型 → 环境 → 客户端（执行默认使用所选环境与客户端） */}
            <div className={styles.ctxBar}>
              <span className={styles.ctxLabel}>项目</span>
              <Select
                size="small"
                style={{ width: 150 }}
                value={activeConv?.project_id ?? undefined}
                placeholder="选择项目"
                options={projects.map((p) => ({ label: p.name, value: p.id }))}
                onChange={(v) => patchConversation({ project_id: v })}
              />
              <span className={styles.ctxLabel}>用例类型</span>
              <Radio.Group
                type="button"
                size="small"
                value={(activeConv?.case_kind as string) || 'ui'}
                onChange={(v) => patchConversation({ case_kind: v })}
              >
                <Radio value="ui">UI 用例</Radio>
                <Radio value="functional">功能用例</Radio>
                <Radio value="api">接口用例</Radio>
              </Radio.Group>
              <span className={styles.ctxLabel}>环境</span>
              <Select
                size="small"
                style={{ width: 190 }}
                value={(activeConv?.environment_id as number) ?? undefined}
                placeholder="默认环境"
                options={envs.map((e) => ({
                  label: `${e.name}${e.is_default ? '（默认）' : ''}`,
                  value: e.id,
                }))}
                onChange={(v) => patchConversation({ environment_id: v })}
                allowClear
                onClear={() => patchConversation({ environment_id: -1 })}
              />
              <span className={styles.ctxLabel}>客户端</span>
              <Select
                size="small"
                style={{ width: 190 }}
                value={(activeConv?.agent_id as number) ?? 0}
                options={[
                  { label: '服务端', value: 0 },
                  ...agents.map((a) => ({
                    label: `${a.name}${a.status === 'online' ? '' : '（离线）'}${pcClient?.agent_name && pcClient.agent_name === a.name ? '（当前 PC）' : ''}`,
                    value: a.id,
                  })),
                ]}
                onChange={(v) => patchConversation({ agent_id: v === 0 ? -1 : v })}
              />
            </div>
            <div className={styles.messages} ref={msgListRef}>
              {loadingMsgs && <Spin style={{ display: 'block', margin: '12px auto' }} />}
              {messages.map((m) => (
                <div className={`${styles.msgRow} ${m.role === 'user' ? styles.user : ''}`} key={m.id}>
                  <div style={{ maxWidth: '76%' }}>
                    {(() => {
                      const live = stream && stream.targetId === m.id ? stream : null;
                      if (m.role === 'user' && editingId === m.id) {
                        return (
                          <div className={styles.editBox}>
                            <Input.TextArea
                              value={editText}
                              onChange={setEditText}
                              autoSize={{ minRows: 2, maxRows: 8 }}
                              placeholder="修改这条消息…"
                            />
                            <div className={styles.editActions}>
                              <Text type="secondary" style={{ fontSize: 12 }}>
                                保存后从这条消息重新生成，其后的消息会被清除
                              </Text>
                              <div>
                                <Button size="small" onClick={() => setEditingId(null)}>取消</Button>
                                <Button
                                  size="small"
                                  type="primary"
                                  loading={sending}
                                  style={{ marginLeft: 8 }}
                                  onClick={() => saveEdit(m.id)}
                                >
                                  保存并重新生成
                                </Button>
                              </div>
                            </div>
                          </div>
                        );
                      }
                      const thinkingText = live ? live.thinking : (m.role === 'assistant' ? m.thinking : null);
                      const showThinking = m.role === 'assistant' && (thinkingText || (live && !live.reply));
                      return (
                        <>
                          {showThinking ? (
                            <details
                              className={styles.thinking}
                              {...(live
                                ? {
                                    open: streamOpen,
                                    onToggle: (e: React.SyntheticEvent<HTMLDetailsElement>) =>
                                      setStreamOpen(e.currentTarget.open),
                                  }
                                : {})}
                            >
                              <summary className={styles.thinkingSummary}>
                                <IconRobot />
                                {live ? (live.reply ? '思考过程' : '思考中…') : '思考过程'}
                              </summary>
                              <div className={styles.thinkingBody}>
                                {thinkingText}
                                {live && !live.reply ? <span className={styles.cursor}>▍</span> : null}
                              </div>
                            </details>
                          ) : null}
                          {live ? (
                            live.reply ? (
                              <div className={`${styles.bubble} ${styles.assistantBubble}`}>
                                {live.reply}
                                <span className={styles.cursor}>▍</span>
                              </div>
                            ) : null
                          ) : (
                            <div className={`${styles.bubble} ${m.role === 'user' ? styles.userBubble : styles.assistantBubble}`}>
                              {m.content}
                            </div>
                          )}
                        </>
                      );
                    })()}
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
                    {m.role === 'user' && editingId !== m.id && (
                      <div className={styles.msgTools} style={{ justifyContent: 'flex-end' }}>
                        <span
                          className={styles.msgTool}
                          onClick={() => {
                            setEditingId(m.id);
                            setEditText(m.content || '');
                          }}
                        >
                          <IconEdit style={{ marginRight: 2 }} />编辑
                        </span>
                      </div>
                    )}
                    {(m.cards || []).map((c, i) => renderCard(c, i, m.id))}
                    {m.role === 'assistant' && (m.actions || []).filter((a: any) => a && a.type === 'tool').length > 0 && (
                      <details className={styles.toolHistory}>
                        <summary className={styles.toolHistorySummary}>
                          <IconRobot />
                          工具调用 × {(m.actions || []).filter((a: any) => a && a.type === 'tool').length}
                        </summary>
                        <div className={styles.toolHistoryBody}>
                          {(m.actions || []).filter((a: any) => a && a.type === 'tool').map((a: any, i: number) => (
                            <div key={i} className={styles.toolRowWrap}>
                              <div
                                className={`${styles.toolRow} ${a.excerpt ? styles.toolRowClickable : ''}`}
                                onClick={() => {
                                  if (a.excerpt) {
                                    const key = `${m.id}:${i}`;
                                    setToolOpen((prev) => ({ ...prev, [key]: !prev[key] }));
                                  }
                                }}
                              >
                                <span className={styles.toolName}>{String(a.tool || '')}</span>
                                <span className={`${styles.toolStatus} ${a.status === 'ok' ? styles.toolOk : styles.toolFail}`}>
                                  {a.status === 'ok' ? '已完成' : '失败'}
                                </span>
                                {a.summary ? <span className={styles.toolArgs}>{String(a.summary)}</span> : null}
                                {a.excerpt ? (
                                  <span className={styles.toolChevron}>{toolOpen[`${m.id}:${i}`] ? '▾' : '▸'}</span>
                                ) : null}
                              </div>
                              {a.excerpt && toolOpen[`${m.id}:${i}`] ? (
                                <pre className={styles.toolExcerpt}>
                                  {String(a.excerpt)}
                                  {a.truncated ? '\n…（已截断，展示节选）' : ''}
                                </pre>
                              ) : null}
                            </div>
                          ))}
                        </div>
                      </details>
                    )}
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
              {stream && stream.targetId == null && (
                <div className={styles.msgRow}>
                  <div style={{ maxWidth: '76%' }}>
                    {liveTools.length > 0 && (
                      <div className={styles.toolRows}>
                        {liveTools.map((t) => (
                          <div key={t.callId} className={styles.toolRowWrap}>
                            <div
                              className={`${styles.toolRow} ${t.excerpt ? styles.toolRowClickable : ''}`}
                              onClick={() => {
                                if (t.excerpt) {
                                  setToolOpen((prev) => ({ ...prev, [t.callId]: !prev[t.callId] }));
                                }
                              }}
                            >
                              <span className={styles.toolName}>{t.tool}</span>
                              <span className={styles.toolArgs}>{t.argsDigest}</span>
                              <span className={`${styles.toolStatus} ${t.status === 'running' ? styles.toolRunning : t.status === 'ok' ? styles.toolOk : styles.toolFail}`}>
                                {t.status === 'running'
                                  ? '执行中…'
                                  : t.status === 'ok'
                                    ? `已完成${t.durationMs ? ` · ${(t.durationMs / 1000).toFixed(1)}s` : ''}`
                                    : '失败'}
                              </span>
                              {t.excerpt ? (
                                <span className={styles.toolChevron}>{toolOpen[t.callId] ? '▾' : '▸'}</span>
                              ) : null}
                            </div>
                            {t.excerpt && toolOpen[t.callId] ? (
                              <pre className={styles.toolExcerpt}>
                                {t.excerpt}
                                {t.truncated ? '\n…（已截断，展示节选）' : ''}
                              </pre>
                            ) : null}
                          </div>
                        ))}
                      </div>
                    )}
                    {(stream.thinking || !stream.reply) && (
                      <details
                        className={styles.thinking}
                        open={streamOpen}
                        onToggle={(e: React.SyntheticEvent<HTMLDetailsElement>) => setStreamOpen(e.currentTarget.open)}
                      >
                        <summary className={styles.thinkingSummary}>
                          <IconRobot />
                          {stream.reply ? '思考过程' : '思考中…'}
                        </summary>
                        <div className={styles.thinkingBody}>
                          {stream.thinking}
                          {!stream.reply ? <span className={styles.cursor}>▍</span> : null}
                        </div>
                      </details>
                    )}
                    {stream.reply ? (
                      <div className={`${styles.bubble} ${styles.assistantBubble}`}>
                        {stream.reply}
                        <span className={styles.cursor}>▍</span>
                      </div>
                    ) : null}
                  </div>
                </div>
              )}
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
                  accept=".md,.docx,.pdf,.xlsx,.png,.jpg,.jpeg"
                  onChange={(e) => {
                    const f = e.target.files?.[0];
                    if (f) uploadFile(f);
                    e.target.value = '';
                  }}
                />
                {sending ? (
                  <Button type="primary" status="danger" icon={<IconStop />} onClick={stopTurn}>
                    停止
                  </Button>
                ) : (
                  <Button type="primary" icon={<IconSend />} onClick={send}>
                    发送
                  </Button>
                )}
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
