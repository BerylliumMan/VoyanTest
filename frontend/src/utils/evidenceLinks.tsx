import React from 'react';

/**
 * B2/036 运行证据：把报告步骤文本中的证据文件路径
 * （reports/xxx.webm|pdf|zip|csv|json|txt|xlsx|docx|log|bin）
 * 渲染为可点击链接（视频证据、抓包、fetch 下载存证等）。
 */
const EVIDENCE_RE =
  /(reports\/[^\s;)]+\.(?:webm|pdf|zip|csv|json|txt|xlsx|docx|log|bin))/g;

export function renderEvidenceLinks(text: string | undefined | null): React.ReactNode {
  const value = String(text || '');
  if (!value) return value;
  const matches = value.match(EVIDENCE_RE);
  if (!matches?.length) return value;

  const re = new RegExp(EVIDENCE_RE.source, 'g');
  const parts: React.ReactNode[] = [];
  let last = 0;
  let m: RegExpExecArray | null;
  let key = 0;
  while ((m = re.exec(value)) !== null) {
    if (m.index > last) parts.push(value.slice(last, m.index));
    const path = m[1];
    parts.push(
      <a
        key={`evidence-${key++}`}
        href={`/${path}`}
        target="_blank"
        rel="noreferrer"
        onClick={(e) => e.stopPropagation()}
      >
        {path.split('/').pop()}
      </a>,
    );
    last = m.index + path.length;
  }
  if (last < value.length) parts.push(value.slice(last));
  return parts;
}

export default renderEvidenceLinks;
