import React, { useEffect, useState } from 'react';
import { Card, Table, Input, Select, Button, Space, DatePicker, Message } from '@arco-design/web-react';
import { IconSearch, IconRefresh } from '@arco-design/web-react/icon';
import axios from 'axios';
import useLocale from '@/utils/useLocale';
import logger from '@/utils/logger';
import styles from './style/index.module.less';

const { RangePicker } = DatePicker;

interface AuditLog {
  id: number;
  action: string;
  user_id: number;
  username: string;
  details: string;
  created_at: string;
  ip_address?: string;
}

interface User {
  id: number;
  username: string;
}

interface Filters {
  user_id?: number;
  action?: string;
  date_from?: string;
  date_to?: string;
}

function AuditLogs() {
  const t = useLocale();
  const [loading, setLoading] = useState(false);
  const [data, setData] = useState<AuditLog[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [size, setSize] = useState(20);
  const [users, setUsers] = useState<User[]>([]);
  const [filters, setFilters] = useState<Filters>({});

  const fetchData = (p?: number, s?: number) => {
    setLoading(true);
    const params: Record<string, string | number> = { page: p ?? page, size: s ?? size };
    if (filters.user_id) params.user_id = filters.user_id;
    if (filters.action) params.action = filters.action;
    if (filters.date_from) params.date_from = filters.date_from;
    if (filters.date_to) params.date_to = filters.date_to;
    axios.get('/api/audit-logs/', { params })
      .then((res) => {
        setData(res.data.items || []);
        setTotal(res.data.total || 0);
      })
      .catch((err) => Message.error(err?.response?.data?.detail || '操作失败'))
      .finally(() => setLoading(false));
  };

  const fetchUsers = () => {
    axios.get('/api/users/').then((res) => setUsers(res.data || [])).catch((err) => { logger.error('Failed to load users:', err); });
  };

  useEffect(() => { fetchUsers(); }, []);

  useEffect(() => { fetchData(); }, [page, size]);

  const handleSearch = () => { setPage(1); fetchData(1, size); };

  const handleReset = () => {
    setFilters({});
    setPage(1);
  };

  const handleDateChange = (dateStrings: string[]) => {
    setFilters((prev: Filters) => ({
      ...prev,
      date_from: dateStrings[0] || undefined,
      date_to: dateStrings[1] || undefined,
    }));
  };

  const columns = [
    { title: t['audit.time'], dataIndex: 'created_at', width: 170, render: (v: string) => v ? new Date(v).toLocaleString() : '-' },
    { title: t['audit.user'], dataIndex: 'username', width: 120, render: (v: string) => v || '-' },
    { title: t['audit.action'], dataIndex: 'action', width: 150 },
    { title: t['audit.detail'], dataIndex: 'details', render: (v: string) => v || '-' },
    { title: t['audit.ip'], dataIndex: 'ip_address', width: 140, render: (v: string) => v || '-' },
  ];

  const pageSizeOptions = [10, 20, 50, 100];

  return (
    <div>
      <Card>
        <Space className={styles['filter-bar']} wrap>
          <Select
            placeholder={t['audit.user.placeholder']}
            className={styles['filter-select']}
            value={filters.user_id}
            onChange={(v) => setFilters((prev: Filters) => ({ ...prev, user_id: v }))}
            allowClear
          >
            {users.map((u) => (
              <Select.Option key={u.id} value={u.id}>{u.username}</Select.Option>
            ))}
          </Select>
          <Input
            placeholder={t['audit.action.placeholder']}
            className={styles['filter-select']}
            value={filters.action || ''}
            onChange={(v) => setFilters((prev: Filters) => ({ ...prev, action: v }))}
          />
          <RangePicker
            className={styles['filter-date-range']}
            onChange={handleDateChange}
          />
          <Button type="primary" icon={<IconSearch />} onClick={handleSearch}>{t['search']}</Button>
          <Button icon={<IconRefresh />} onClick={handleReset}>{t['reset']}</Button>
        </Space>
        <Table
          columns={columns}
          data={data}
          rowKey="id"
          loading={loading}
          pagination={{
            total,
            current: page,
            pageSize: size,
            showTotal: true,
            sizeOptions: pageSizeOptions,
            onChange: (p, s) => { setPage(p); setSize(s); },
          }}
        />
      </Card>
    </div>
  );
}

export default AuditLogs;
