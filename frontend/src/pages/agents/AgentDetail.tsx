import React, { useEffect, useState } from 'react';
import { useParams } from 'react-router-dom';
import { Card, Descriptions, Tag, Spin, Table, Button, Space, Message } from '@arco-design/web-react';
import { IconArrowLeft } from '@arco-design/web-react/icon';
import axios from 'axios';
import styles from './style/index.module.less';

interface AgentDetail {
  id: number;
  name: string;
  endpoint: string;
  status: string;
  description: string;
  last_heartbeat: string;
}

interface AgentStats {
  total_runs: number;
}

function AgentDetail() {
  const { id } = useParams<{ id: string }>();
  const [agent, setAgent] = useState<AgentDetail | null>(null);
  const [stats, setStats] = useState<AgentStats | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    Promise.all([
      axios.get(`/api/agents/${id}`).then((r) => setAgent(r.data)),
      axios.get(`/api/agents/${id}/stats`).then((r) => setStats(r.data)),
    ]).catch(() => Message.error('加载失败')).finally(() => setLoading(false));
  }, [id]);

  if (loading) return <Spin loading className={styles['spin-center']} />;

  return (
    <div>
      <Button type="text" icon={<IconArrowLeft />} onClick={() => window.history.back()}>返回</Button>
      <Card title={`Agent: ${agent?.name || id}`}>
        <Descriptions
          data={[
            { label: '名称', value: agent?.name || '-' },
            { label: '端点', value: agent?.endpoint || '-' },
            { label: '状态', value: <Tag color={agent?.status === 'online' ? 'green' : 'gray'}>{agent?.status || '-'}</Tag> },
            { label: '描述', value: agent?.description || '-' },
            { label: '最后心跳', value: agent?.last_heartbeat ? new Date(agent.last_heartbeat).toLocaleString() : '-' },
            { label: '总运行次数', value: stats?.total_runs ?? 0 },
          ]}
          column={2}
        />
      </Card>
    </div>
  );
}

export default AgentDetail;
