import { describe, expect, it } from 'vitest';
import { buildCurl, buildJs, buildPython } from '../pages/api_test/utils/snippets';

describe('031 US7 snippets', () => {
  const jsonSrc = {
    method: 'POST',
    url: 'http://mock.local/api/login',
    headers: {
      'Content-Type': 'application/json',
      Authorization: 'Bearer ***',
      'Content-Length': '21',
    },
    body_preview: '{"username":"admin"}',
  };

  it('curl：含方法/URL/请求头/正文，剔除 content-length', () => {
    const s = buildCurl(jsonSrc);
    expect(s).toContain("curl -X POST 'http://mock.local/api/login'");
    expect(s).toContain("-H 'Authorization: Bearer ***'");
    expect(s).toContain("--data-raw '{\"username\":\"admin\"}'");
    expect(s).not.toContain('content-length');
  });

  it('curl：multipart 时跳过 content-type 并提示 -F', () => {
    const s = buildCurl({
      method: 'POST',
      url: 'http://mock.local/api/upload',
      headers: { 'Content-Type': 'multipart/form-data; boundary=xyz' },
      body_preview: null,
    });
    expect(s).toContain("-F 'file=@/path/to/file'");
    expect(s).not.toContain('multipart/form-data; boundary');
  });

  it('python：requests 形态且剔除 content-length', () => {
    const s = buildPython(jsonSrc);
    expect(s).toContain('import requests');
    expect(s).toContain('requests.request(');
    expect(s).toContain('"Authorization": "Bearer ***"');
    expect(s).not.toContain('Content-Length');
    expect(s).toContain('timeout=30');
  });

  it('js：fetch 形态', () => {
    const s = buildJs(jsonSrc);
    expect(s).toContain('await fetch(');
    expect(s).toContain('method: "POST"');
    expect(s).toContain('console.log(resp.status');
  });
});
