/**
 * 031（US7 T031）代码片段生成器：curl / Python / JavaScript。
 *
 * 契约（contracts §1.5）：基于**渲染后**请求生成；请求头已脱敏（secret 显示 ****** 或 ***）；
 * 变量已替换为具体值，可直接粘贴到终端复现（资产本地化：不依赖任何外部服务）。
 */
export interface SnippetSource {
  method: string;
  url: string;
  headers?: Record<string, string>;
  body_preview?: string | null;
}

const isMultipart = (headers: Record<string, string>): boolean =>
  Object.entries(headers).some(
    ([k, v]) => k.toLowerCase() === 'content-type' && v.toLowerCase().includes('multipart')
  );

/** curl：含 -X/-H/--data-raw；multipart 时给出 -F 提示占位 */
export const buildCurl = (src: SnippetSource): string => {
  const headers = src.headers || {};
  const parts: string[] = [`curl -X ${src.method.toUpperCase()} '${src.url}'`];
  for (const [k, v] of Object.entries(headers)) {
    if (isMultipart(headers) && k.toLowerCase() === 'content-type') continue;
    if (k.toLowerCase() === 'content-length') continue;
    parts.push(`  -H '${k}: ${v.replace(/'/g, "'\\''")}'`);
  }
  if (isMultipart(headers)) {
    parts.push(`  -F 'file=@/path/to/file'   # multipart：按需补充各字段与文件`);
  } else if (src.body_preview) {
    parts.push(`  --data-raw '${src.body_preview.replace(/'/g, "'\\''")}'`);
  }
  return parts.join(' \\\n');
};

/** Python（requests） */
export const buildPython = (src: SnippetSource): string => {
  const headers = src.headers || {};
  const headerLines = Object.entries(headers)
    .filter(([k]) => k.toLowerCase() !== 'content-length')
    .map(([k, v]) => `    "${k}": "${v}",`)
    .join('\n');
  const lines = ['import requests', '', 'resp = requests.request('];
  lines.push(`    "${src.method.toUpperCase()}",`);
  lines.push(`    "${src.url}",`);
  if (headerLines) lines.push(`    headers={\n${headerLines}\n    },`);
  if (isMultipart(headers)) {
    lines.push(`    files={"file": open("/path/to/file", "rb")},`);
  } else if (src.body_preview) {
    lines.push(`    data="""${src.body_preview}""",`);
  }
  lines.push('    timeout=30,');
  lines.push(')');
  lines.push('print(resp.status_code, resp.text[:500])');
  return lines.join('\n');
};

/** JavaScript（fetch / Node 18+） */
export const buildJs = (src: SnippetSource): string => {
  const headers = src.headers || {};
  const headerObj = Object.entries(headers)
    .filter(([k]) => k.toLowerCase() !== 'content-length')
    .map(([k, v]) => `    "${k}": "${v}",`)
    .join('\n');
  const lines = ['const resp = await fetch('];
  lines.push(`  "${src.url}",`);
  lines.push('  {');
  lines.push(`    method: "${src.method.toUpperCase()}",`);
  if (headerObj) lines.push(`    headers: {\n${headerObj}\n    },`);
  if (isMultipart(headers)) {
    lines.push('    body: formData, // multipart：自行构造 FormData 并 append 字段/文件');
  } else if (src.body_preview) {
    lines.push(`    body: ${JSON.stringify(src.body_preview)},`);
  }
  lines.push('  }');
  lines.push(');');
  lines.push('console.log(resp.status, await resp.text());');
  return lines.join('\n');
};

export const buildSnippet = (
  kind: 'curl' | 'python' | 'js',
  src: SnippetSource
): string => {
  if (kind === 'python') return buildPython(src);
  if (kind === 'js') return buildJs(src);
  return buildCurl(src);
};
