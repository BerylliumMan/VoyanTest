import React, { useState } from 'react';
import { Button, Input, Message, Space, Tooltip } from '@arco-design/web-react';
import { IconPlayArrow } from '@arco-design/web-react/icon';
import CodeMirror from '@uiw/react-codemirror';
import { apiPost } from '@/utils/apiRequest';
import { ApiStep } from '../types';
import styles from '../style/index.module.less';

interface ScriptPanelProps {
  spec: ApiStep;
  onChange: (patch: Partial<ApiStep>) => void;
}

const SCRIPT_HINT =
  "可用：vars（变量）· json/base64/hashlib/re/time/random/urllib_parse · hmac_sha256(key, msg) · hex/unhex/b64encode · " +
  "AES-GCM/CBC、RSA PKCS1/PSS、EC 签名验签；支持 赋值/if/for(≤1e4)；禁用 import/while/open 等";

const POST_HINT =
  "后置额外可用：status_code · duration_ms · headers · body（解析后响应体）· body_text；写在脚本里的新变量会覆盖写回";

interface TryResult {
  ok: boolean;
  variables?: Record<string, unknown>;
  output?: string;
  error?: string;
}

/** 031（US3）：前后置脚本编辑器（试跑零出站 —— 只跑沙箱，不发请求）。 */
const ScriptPanel: React.FC<ScriptPanelProps> = ({ spec, onChange }) => {
  const [trying, setTrying] = useState<'pre' | 'post' | null>(null);
  const [tryResult, setTryResult] = useState<TryResult | null>(null);

  const tryRun = async (phase: 'pre' | 'post') => {
    const script = phase === 'pre' ? spec.pre_script : spec.post_script;
    if (!script || !script.trim()) {
      Message.warning('脚本为空');
      return;
    }
    setTrying(phase);
    try {
      const data = await apiPost<TryResult>('/api/api-test/debug/script', {
        script,
        phase,
        variables: {},
        response:
          phase === 'post'
            ? { status_code: 200, duration_ms: 12, headers: {}, body: { sample: true }, body_text: '{"sample":true}' }
            : undefined,
        timeout_ms: spec.script_timeout_ms ?? undefined,
      });
      setTryResult(data);
    } catch {
      /* apiRequest 已弹错误 */
    } finally {
      setTrying(null);
    }
  };

  const renderEditor = (phase: 'pre' | 'post') => {
    const value = (phase === 'pre' ? spec.pre_script : spec.post_script) || '';
    return (
      <div className={styles.scriptSection}>
        <div className={styles.prePostTitle}>
          {phase === 'pre' ? '前置脚本（请求发送前执行）' : '后置脚本（响应后执行）'}
          <Tooltip content={phase === 'pre' ? SCRIPT_HINT : `${SCRIPT_HINT}；${POST_HINT}`}>
            <span className={styles.scriptHintMark}>?</span>
          </Tooltip>
          <Button
            size="mini"
            type="text"
            icon={<IconPlayArrow />}
            loading={trying === phase}
            onClick={() => tryRun(phase)}
          >
            试跑
          </Button>
        </div>
        <div className={styles.scriptEditor}>
          <CodeMirror
            value={value}
            height="120px"
            basicSetup={{ lineNumbers: true, foldGutter: false }}
            onChange={(text) => onChange(phase === 'pre' ? { pre_script: text } : { post_script: text })}
          />
        </div>
        <Space style={{ marginTop: 6 }} wrap>
          <span className={styles.scriptMeta}>超时（毫秒，留空用系统默认）</span>
          <Input
            size="small"
            style={{ width: 120 }}
            value={spec.script_timeout_ms == null ? '' : String(spec.script_timeout_ms)}
            onChange={(text) => {
              const num = Number(text);
              onChange({
                script_timeout_ms: text.trim() === '' || Number.isNaN(num) ? null : Math.min(10000, Math.max(200, num)),
              });
            }}
          />
        </Space>
      </div>
    );
  };

  return (
    <div className={styles.panelList}>
      {renderEditor('pre')}
      {renderEditor('post')}
      {tryResult && (
        <div className={styles.scriptTryResult}>
          <div>
            <b>试跑结果：</b>
            {tryResult.ok ? <span style={{ color: 'var(--vt-success)' }}>通过</span> : <span style={{ color: 'var(--vt-error)' }}>失败</span>}
          </div>
          {tryResult.error && <div className={styles.scriptError}>错误：{tryResult.error}</div>}
          {tryResult.output && <pre className={styles.scriptPre}>输出：{tryResult.output}</pre>}
          {tryResult.variables && Object.keys(tryResult.variables).length > 0 && (
            <pre className={styles.scriptPre}>
              写回变量：{JSON.stringify(tryResult.variables, null, 2)}
            </pre>
          )}
        </div>
      )}
    </div>
  );
};

export default ScriptPanel;
