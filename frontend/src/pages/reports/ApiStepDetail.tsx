import React, { useState } from 'react';
import { Button, Collapse, Table, Tag } from '@arco-design/web-react';
import { IconCheckCircleFill, IconCloseCircleFill } from '@arco-design/web-react/icon';
import useLocale from '@/utils/useLocale';
import styles from './style/index.module.less';
import type { StepDetail } from './types';

// 大文本截断阈值：超过 2000 字符折叠，提供「查看完整」展开
const MAX_BODY_CHARS = 2000;

/** 状态码颜色：2xx 绿 / 4xx 橙 / 5xx 红 */
const statusColor = (status: number): string => {
  if (status >= 200 && status < 300) return 'green';
  if (status >= 400 && status < 500) return 'orangered';
  if (status >= 500) return 'red';
  return 'blue';
};

/** 字节数 → 可读大小 */
const formatSize = (size?: number): string => {
  if (size == null) return '';
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / 1024 / 1024).toFixed(2)} MB`;
};

/** headers 归一化：对象或数组 → [{key, value}] */
const normalizeHeaders = (headers: unknown): Array<{ key: string; value: string }> => {
  if (!headers) return [];
  if (Array.isArray(headers)) {
    return headers.map((h) => ({
      key: String((h as { key?: unknown })?.key ?? ''),
      value: String((h as { value?: unknown })?.value ?? ''),
    }));
  }
  if (typeof headers === 'object') {
    return Object.entries(headers as Record<string, unknown>).map(([key, value]) => ({
      key,
      value: typeof value === 'string' ? value : JSON.stringify(value),
    }));
  }
  return [];
};

/** body 归一化：对象 → 格式化 JSON 字符串 */
const normalizeBody = (body: unknown): string => {
  if (body == null) return '';
  if (typeof body === 'string') return body;
  try {
    return JSON.stringify(body, null, 2);
  } catch {
    return String(body);
  }
};

/** 可折叠大文本：超过阈值时截断 + 「查看完整 / 收起」 */
const CollapsibleText: React.FC<{ text: string }> = ({ text }) => {
  const t = useLocale();
  const [expanded, setExpanded] = useState(false);
  if (text.length <= MAX_BODY_CHARS) {
    return <pre className={styles.apiBody}>{text}</pre>;
  }
  return (
    <div>
      <pre className={styles.apiBody}>{expanded ? text : `${text.slice(0, MAX_BODY_CHARS)}…`}</pre>
      <Button size="mini" type="text" onClick={() => setExpanded((v) => !v)}>
        {expanded ? t['api.collapse'] : t['api.view.full']}
      </Button>
    </div>
  );
};

/**
 * 接口用例步骤明细：请求 / 响应 / 断言表 / 提取表。
 * 仅当步骤携带 request/response/assertions/extracted 任一字段时渲染；
 * UI 用例（无这些字段）返回 null，渲染路径与原来完全一致。
 */
const ApiStepDetail: React.FC<{ step: StepDetail }> = ({ step }) => {
  const t = useLocale();
  const { request, response, assertions, extracted } = step;
  if (!request && !response && !assertions && !extracted) return null;

  const reqHeaders = normalizeHeaders(request?.headers);
  const reqBody = normalizeBody(request?.body);
  const resHeaders = normalizeHeaders(response?.headers);
  const resBody = normalizeBody(response?.body);

  return (
    <div className={styles.apiSection}>
      {request && (
        <div className={styles.apiBlock}>
          <div className={styles.apiBlockTitle}>{t['api.request']}</div>
          <div className={styles.apiRequestLine}>
            {request.method && <Tag color="blue" className={styles.apiMethodTag}>{request.method}</Tag>}
            {request.url && <span className={styles.apiUrl}>{request.url}</span>}
          </div>
          {(reqHeaders.length > 0 || reqBody) && (
            <Collapse bordered={false} className={styles.apiCollapse}>
              {reqHeaders.length > 0 && (
                <Collapse.Item header={t['api.headers']} name="req-headers">
                  <pre className={styles.apiBody}>{reqHeaders.map((h) => `${h.key}: ${h.value}`).join('\n')}</pre>
                </Collapse.Item>
              )}
              {reqBody && (
                <Collapse.Item header={t['api.body']} name="req-body">
                  <CollapsibleText text={reqBody} />
                </Collapse.Item>
              )}
            </Collapse>
          )}
        </div>
      )}

      {response && (
        <div className={styles.apiBlock}>
          <div className={styles.apiBlockTitle}>{t['api.response']}</div>
          <div className={styles.apiResponseMeta}>
            {response.status != null && <Tag color={statusColor(response.status)}>{response.status}</Tag>}
            {response.duration_ms != null && (
              <span className={styles.apiMetaText}>{t['api.duration']}: {response.duration_ms}ms</span>
            )}
            {formatSize(response.size) && (
              <span className={styles.apiMetaText}>{t['api.size']}: {formatSize(response.size)}</span>
            )}
          </div>
          {(resHeaders.length > 0 || resBody) && (
            <Collapse bordered={false} className={styles.apiCollapse}>
              {resHeaders.length > 0 && (
                <Collapse.Item header={t['api.response.headers']} name="res-headers">
                  <pre className={styles.apiBody}>{resHeaders.map((h) => `${h.key}: ${h.value}`).join('\n')}</pre>
                </Collapse.Item>
              )}
              {resBody && (
                <Collapse.Item header={t['api.response.body']} name="res-body">
                  <CollapsibleText text={resBody} />
                </Collapse.Item>
              )}
            </Collapse>
          )}
        </div>
      )}

      {assertions && assertions.length > 0 && (
        <div className={styles.apiBlock}>
          <div className={styles.apiBlockTitle}>{t['api.assertions']}</div>
          <Table
            size="small"
            pagination={false}
            rowKey="__rowKey"
            rowClassName={(record) => (record.passed === false ? styles.apiAssertionRowFail : '')}
            data={assertions.map((a, i) => ({ ...a, __rowKey: `a-${i}` }))}
            columns={[
              { title: t['api.assertion.type'], dataIndex: 'type', width: 110, render: (v: unknown) => v || '--' },
              { title: t['api.assertion.condition'], dataIndex: 'condition', width: 110, render: (v: unknown) => v || '--' },
              {
                title: t['api.assertion.expected'], dataIndex: 'expected',
                render: (v: unknown, row: { passed?: boolean }) => (
                  <span className={row.passed === false ? styles.apiExpectedFail : undefined}>{v ?? '--'}</span>
                ),
              },
              {
                title: t['api.assertion.actual'], dataIndex: 'actual',
                render: (v: unknown, row: { passed?: boolean }) => (
                  <span className={row.passed === false ? styles.apiActualFail : undefined}>{v ?? '--'}</span>
                ),
              },
              {
                title: t['api.assertion.result'], dataIndex: 'passed', width: 220,
                render: (v: unknown, row: { error?: string }) => {
                  if (v === true) {
                    return <span className={styles.apiPassed}><IconCheckCircleFill /> {t['api.passed']}</span>;
                  }
                  if (v === false) {
                    return (
                      <span>
                        <span className={styles.apiFailed}><IconCloseCircleFill /> {t['api.failed']}</span>
                        {row.error && <span className={styles.apiAssertionError}>{row.error}</span>}
                      </span>
                    );
                  }
                  return '--';
                },
              },
            ]}
          />
        </div>
      )}

      {extracted && extracted.length > 0 && (
        <div className={styles.apiBlock}>
          <div className={styles.apiBlockTitle}>{t['api.extracted']}</div>
          <Table
            size="small"
            pagination={false}
            rowKey="__rowKey"
            data={extracted.map((e, i) => ({ ...e, __rowKey: `e-${i}` }))}
            columns={[
              { title: t['api.extracted.variable'], dataIndex: 'variable', render: (v: unknown) => v || '--' },
              { title: t['api.extracted.scope'], dataIndex: 'scope', width: 120, render: (v: unknown) => v || '--' },
              { title: t['api.extracted.value'], dataIndex: 'value', render: (v: unknown) => v ?? '--' },
              {
                title: t['api.extracted.result'], dataIndex: 'ok', width: 220,
                render: (v: unknown, row: { error?: string }) => {
                  if (v === true) {
                    return <span className={styles.apiPassed}><IconCheckCircleFill /> {t['api.passed']}</span>;
                  }
                  if (v === false) {
                    return (
                      <span>
                        <span className={styles.apiFailed}><IconCloseCircleFill /> {t['api.failed']}</span>
                        {row.error && <span className={styles.apiAssertionError}>{row.error}</span>}
                      </span>
                    );
                  }
                  return '--';
                },
              },
            ]}
          />
        </div>
      )}
    </div>
  );
};

export default ApiStepDetail;