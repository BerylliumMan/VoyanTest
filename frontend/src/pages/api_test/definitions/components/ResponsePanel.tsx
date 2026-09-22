import React, { useState } from 'react';
import { Empty, Spin, Tag, Typography } from '@arco-design/web-react';
import { DebugResponse } from '../types';
import styles from '../style/index.module.less';

const { Text } = Typography;

interface ResponsePanelProps {
  response: DebugResponse | null;
  loading?: boolean;
}

/** 状态码颜色 */
const statusColor = (status: number): string => {
  if (status >= 200 && status < 300) return 'green';
  if (status >= 300 && status < 400) return 'arcoblue';
  if (status >= 400 && status < 500) return 'orange';
  return 'red';
};

/** 格式化大小 */
const formatSize = (size: number): string => {
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / 1024 / 1024).toFixed(2)} MB`;
};

/**
 * 响应面板：状态码 / 耗时 / 大小 / 响应头 / 响应体 / 断言结果。
 * 对齐 contract §1.4 debug 响应结构。
 */
const ResponsePanel: React.FC<ResponsePanelProps> = ({ response, loading = false }) => {
  const [showHeaders, setShowHeaders] = useState(true);
  const [showBody, setShowBody] = useState(true);

  if (loading) {
    return (
      <div className={styles.responsePanel}>
        <Spin loading tip="发送中..." style={{ width: '100%' }}>
          <div className={styles.responseEmpty}>正在发送请求...</div>
        </Spin>
      </div>
    );
  }

  if (!response) {
    return (
      <div className={styles.responsePanel}>
        <Empty description="点击「发送」查看响应结果" />
      </div>
    );
  }

  const { rendered, response: resp, assertions, extracted, error } = response;

  return (
    <div className={styles.responsePanel}>
      {/* 渲染后的请求 */}
      <div className={styles.responseSection}>
        <div className={styles.responseSectionTitle}>请求（渲染后）</div>
        <div className={styles.renderedRow}>
          <Tag color="arcoblue">{rendered.method}</Tag>
          <Text className={styles.renderedUrl}>{rendered.url}</Text>
        </div>
        {rendered.body_preview && (
          <pre className={styles.responsePre}>{rendered.body_preview}</pre>
        )}
      </div>

      {/* 错误信息 */}
      {error && (
        <div className={styles.responseSection}>
          <div className={styles.responseError}>{error}</div>
        </div>
      )}

      {/* 响应元信息 */}
      {resp && (
        <div className={styles.responseSection}>
          <div className={styles.responseSectionTitle}>响应</div>
          <div className={styles.responseMeta}>
            <Tag color={statusColor(resp.status)}>{resp.status}</Tag>
            <span className={styles.responseMetaItem}>耗时 {resp.duration_ms} ms</span>
            <span className={styles.responseMetaItem}>大小 {formatSize(resp.size)}</span>
            {resp.truncated && <Tag color="orange">已截断</Tag>}
          </div>

          {/* 响应头 */}
          <div
            className={styles.responseSubTitle}
            onClick={() => setShowHeaders(!showHeaders)}
            role="button"
            tabIndex={0}
            onKeyDown={(e) => e.key === 'Enter' && setShowHeaders(!showHeaders)}
          >
            {showHeaders ? '▾' : '▸'} 响应头 ({Object.keys(resp.headers || {}).length})
          </div>
          {showHeaders && (
            <div className={styles.responseHeaders}>
              {Object.entries(resp.headers || {}).map(([k, v]) => (
                <div className={styles.responseHeaderRow} key={k}>
                  <span className={styles.responseHeaderKey}>{k}:</span>
                  <span className={styles.responseHeaderValue}>{v}</span>
                </div>
              ))}
            </div>
          )}

          {/* 响应体 */}
          <div
            className={styles.responseSubTitle}
            onClick={() => setShowBody(!showBody)}
            role="button"
            tabIndex={0}
            onKeyDown={(e) => e.key === 'Enter' && setShowBody(!showBody)}
          >
            {showBody ? '▾' : '▸'} 响应体
          </div>
          {showBody && (
            <pre className={styles.responsePre}>{resp.body_preview || '(空)'}</pre>
          )}
        </div>
      )}

      {/* 断言结果 */}
      {assertions && assertions.length > 0 && (
        <div className={styles.responseSection}>
          <div className={styles.responseSectionTitle}>断言结果</div>
          {assertions.map((a, i) => (
            <div className={styles.assertionResultRow} key={i}>
              <Tag color={a.passed ? 'green' : 'red'}>{a.passed ? '通过' : '失败'}</Tag>
              <span className={styles.assertionResultName}>{a.name || a.expected}</span>
              {!a.passed && (
                <span className={styles.assertionResultDetail}>
                  期望 {a.expected} / 实际 {a.actual}
                  {a.error ? `（${a.error}）` : ''}
                </span>
              )}
            </div>
          ))}
        </div>
      )}

      {/* 提取结果 */}
      {extracted && extracted.length > 0 && (
        <div className={styles.responseSection}>
          <div className={styles.responseSectionTitle}>提取结果</div>
          {extracted.map((e, i) => (
            <div className={styles.assertionResultRow} key={i}>
              <span className={styles.assertionResultName}>{e.variable}</span>
              <span className={styles.responseHeaderValue}>{e.value_masked}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
};

export default ResponsePanel;