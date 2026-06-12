import React from 'react';
import {
  Card, Table, Input, Button, Space, Spin,
} from '@arco-design/web-react';
import { IconPlus } from '@arco-design/web-react/icon';
import { TestCase } from '../types';
import type { ColumnProps } from '@arco-design/web-react/es/Table';
import styles from '../style/components.module.less';

interface TestCaseTableProps {
  data: TestCase[];
  loading: boolean;
  total: number;
  page: number;
  pageSize: number;
  columns: ColumnProps<TestCase>[];
  selectedRowKeys: number[];
  onSelectionChange: (keys: number[]) => void;
  onPageChange: (page: number, pageSize: number) => void;
  searchQuery: string;
  onSearchChange: (val: string) => void;
  onSearch: (val: string) => void;
  onClearSearch: () => void;
  filterExtra?: React.ReactNode;
  batchActions?: React.ReactNode;
  onCreate: () => void;
  canCreate?: boolean;
  t: Record<string, string>;
}

const TestCaseTable: React.FC<TestCaseTableProps> = ({
  data, loading, total, page, pageSize, columns,
  selectedRowKeys, onSelectionChange, onPageChange,
  searchQuery, onSearchChange, onSearch, onClearSearch,
  filterExtra, batchActions, onCreate, canCreate, t,
}) => {
  return (
    <div className={styles['table-wrapper']}>
      <div className={styles['table-toolbar']}>
        <Input.Search
          placeholder={t['search.placeholder']}
          value={searchQuery}
          onChange={(v) => onSearchChange(v)}
          onSearch={(v) => { onSearch(v || ''); }}
          className={styles['search-input']}
          allowClear
          onClear={onClearSearch}
        />
        {filterExtra}
        <Space>
          {batchActions}
          <Button type="primary" icon={<IconPlus />} onClick={onCreate} disabled={!canCreate}>
            {t['new.case']}
          </Button>
        </Space>
      </div>
      <Card className={styles['table-card']}>
        <Spin loading={loading} className={styles['table-spin']}>
          <Table
            columns={columns} data={data} rowKey="id"
            rowSelection={{
              selectedRowKeys,
              onChange: (_keys) => onSelectionChange(_keys as number[]),
            }}
            pagination={{
              total, current: page, pageSize,
              onChange: onPageChange,
              sizeOptions: [10, 20, 50], sizeCanChange: true,
              showTotal: true,
            }} />
        </Spin>
      </Card>
    </div>
  );
};

export default TestCaseTable;
