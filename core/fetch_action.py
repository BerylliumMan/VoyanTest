"""036 US1：fetch 动作（页面 API 调用）——JS 构建 / value 校验 / 结果解析。

契约（contracts/api-fetch-script.md C1/C2）：
- value 为 JSON：``{method,url,headers?,body?,evidence?,filename?}``
- 页面上下文执行 ``fetch``（``credentials:'include'``，相对 URL 基于 location.origin）
- evidence=true 时响应体 base64 存入 ``window.__vt_fetch[token]``（分块回传协议 C2）
- 结果解析必须**剥 ### Result 块**（项目红线：MCP 响应含源码回显，禁止全文宽松匹配）
"""
from __future__ import annotations

import base64
import json
import os
import re
import time
import uuid

FETCH_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")

# 分块回传：每块 base64 字符数（C2；1MB 原文 ≈ 1.33MB base64）
CHUNK_CHARS = 1024 * 1024
# 结果内嵌内容节选上限（字节）
EXCERPT_BYTES = 8192

_GLOBAL = "__vt_fetch"

# ### Result 块解析（与 coordinate_probe/browser_probe 同款红线实现）
_RESULT_RE = re.compile(r"###\s*Result\s*\n(.+?)(?:\n###|\Z)", re.S)


def validate_fetch_value(value: str) -> dict:
    """解析并校验 fetch 动作的 value JSON；非法输入抛可读 ValueError。"""
    raw = (value or "").strip()
    if not raw:
        raise ValueError("fetch 动作需要 value JSON（method/url）")
    try:
        data = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"fetch value 不是合法 JSON：{exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("fetch value 必须是 JSON 对象")

    method = str(data.get("method") or "").strip().upper()
    if method not in FETCH_METHODS:
        raise ValueError(
            f"fetch method 非法：{method or '(空)'}（可选 {'/'.join(FETCH_METHODS)}）"
        )
    url = str(data.get("url") or "").strip()
    if not url:
        raise ValueError("fetch 需要 url")

    headers = data.get("headers") or {}
    if not isinstance(headers, dict):
        raise ValueError("fetch headers 必须是对象")
    headers = {str(k): str(v) for k, v in headers.items()}

    body = data.get("body")
    if body is not None and not isinstance(body, str):
        raise ValueError("fetch body 必须是字符串")

    filename = str(data.get("filename") or "").strip() or None
    return {
        "method": method,
        "url": url,
        "headers": headers,
        "body": body,
        "evidence": bool(data.get("evidence")),
        "filename": filename,
    }


def build_fetch_js(spec: dict, token: str) -> str:
    """构建 browser_evaluate 用的 async JS 函数字符串（C1）。

    返回 JSON 字符串：``{ok,status,size,content_type,binary,excerpt,error?,stored?}``
    """
    method = json.dumps(spec["method"])
    url = json.dumps(spec["url"])
    headers = json.dumps(spec["headers"] or {})
    body = json.dumps(spec["body"]) if spec.get("body") is not None else "null"
    evidence = "true" if spec.get("evidence") else "false"
    token_lit = json.dumps(token)
    return (
        "async () => {\n"
        "  try {\n"
        f"    const opts = {{ method: {method}, headers: {headers}, credentials: 'include' }};\n"
        f"    const body = {body};\n"
        "    if (body !== null && body !== undefined) opts.body = body;\n"
        f"    const res = await fetch({url}, opts);\n"
        "    const buf = await res.arrayBuffer();\n"
        "    const bytes = new Uint8Array(buf);\n"
        "    const ct = res.headers.get('content-type') || '';\n"
        "    const isText = /text|json|xml|csv|javascript|html/i.test(ct) || ct === '';\n"
        "    let excerpt = '';\n"
        "    if (isText) {\n"
        "      try { excerpt = new TextDecoder('utf-8').decode(bytes.slice(0, " + str(EXCERPT_BYTES) + ")); } catch (e) { excerpt = ''; }\n"
        "    }\n"
        "    const out = { ok: !!res.ok, status: res.status, size: bytes.length, content_type: ct, binary: !isText, excerpt: excerpt };\n"
        f"    if ({evidence}) {{\n"
        "      let bin = '';\n"
        "      const CH = 0x8000;\n"
        "      for (let i = 0; i < bytes.length; i += CH) {\n"
        "        bin += String.fromCharCode.apply(null, bytes.subarray(i, i + CH));\n"
        "      }\n"
        "      let b64 = '';\n"
        "      try { b64 = btoa(bin); } catch (e) { b64 = ''; }\n"
        f"      window.{_GLOBAL} = window.{_GLOBAL} || {{}};\n"
        f"      window.{_GLOBAL}[{token_lit}] = {{ b64: b64, size: bytes.length, content_type: ct }};\n"
        "      out.stored = b64.length;\n"
        "    }\n"
        "    return JSON.stringify(out);\n"
        "  } catch (e) {\n"
        "    return JSON.stringify({ ok: false, status: null, size: 0, content_type: '', binary: false, excerpt: '', error: String((e && e.message) || e) });\n"
        "  }\n"
        "}"
    )


def build_fetch_info_js(token: str) -> str:
    token_lit = json.dumps(token)
    return (
        "() => {\n"
        f"  const s = (window.{_GLOBAL} || {{}})[{token_lit}];\n"
        "  if (!s) return JSON.stringify({ size: 0 });\n"
        "  return JSON.stringify({ size: s.b64.length, content_type: s.content_type });\n"
        "}"
    )


def build_fetch_chunk_js(token: str, offset: int = 0, size: int = CHUNK_CHARS) -> str:
    token_lit = json.dumps(token)
    return (
        "() => {\n"
        f"  const s = (window.{_GLOBAL} || {{}})[{token_lit}];\n"
        "  if (!s) return JSON.stringify({ data: '', eof: true, size: 0 });\n"
        f"  const data = s.b64.substr({int(offset)}, {int(size)});\n"
        f"  return JSON.stringify({{ data: data, eof: ({int(offset)} + data.length) >= s.b64.length, size: s.b64.length }});\n"
        "}"
    )


def build_fetch_release_js(token: str) -> str:
    token_lit = json.dumps(token)
    return (
        "() => {\n"
        f"  if (window.{_GLOBAL}) delete window.{_GLOBAL}[{token_lit}];\n"
        "  return 'released';\n"
        "}"
    )


def parse_fetch_result(text: str) -> dict:
    """从 MCP 响应文本中剥 ### Result 块并解析 fetch 结果 JSON（双解码）。"""
    match = _RESULT_RE.search(text or "")
    if not match:
        raise ValueError("MCP 响应中没有 ### Result 块")
    block = match.group(1).strip()
    try:
        data = json.loads(block)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"fetch 结果 JSON 解析失败：{exc}") from exc
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except (ValueError, TypeError) as exc:
            raise ValueError(f"fetch 结果 JSON 解析失败（双解码）：{exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("fetch 结果不是 JSON 对象")
    return data


# ── 双链共用的执行编排（C1/C2） ──────────────────────────────────────────────


def resolve_max_bytes() -> int:
    """存证上限（字节）：读 settings（客户端进程读不到时默认 100MB）。"""
    try:
        from app.config import get_settings

        mb = int(getattr(get_settings(), "exec_fetch_evidence_max_mb", 100) or 100)
    except Exception:
        mb = 100
    return max(1, mb) * 1024 * 1024


async def _release(call_tool, token: str) -> None:
    try:
        await call_tool("browser_evaluate", {"function": build_fetch_release_js(token)})
    except Exception:  # 释放失败不影响主流程（页面关闭即回收）
        pass


async def _collect_evidence(call_tool, token: str, data: dict, max_bytes: int) -> dict:
    """分块回传响应体（C2）；返回 {data: bytes, size, truncated}。"""
    size_raw = int(data.get("size") or 0)
    total_chars = 0
    try:
        res = await call_tool("browser_evaluate", {"function": build_fetch_info_js(token)})
        if res.get("success"):
            total_chars = int(parse_fetch_result(res.get("text") or "").get("size") or 0)
    except (ValueError, TypeError):
        total_chars = 0

    limit_chars = min(total_chars, (max_bytes * 4 + 2) // 3)
    parts: list[str] = []
    offset = 0
    try:
        while offset < limit_chars:
            res = await call_tool(
                "browser_evaluate",
                {"function": build_fetch_chunk_js(token, offset, CHUNK_CHARS)},
            )
            if not res.get("success"):
                break
            try:
                chunk = parse_fetch_result(res.get("text") or "")
            except ValueError:
                break
            piece = chunk.get("data") or ""
            parts.append(piece)
            offset += len(piece)
            if chunk.get("eof") or not piece:
                break
    finally:
        await _release(call_tool, token)

    b64 = "".join(parts)
    try:
        raw = base64.b64decode(b64 + "=" * (-len(b64) % 4))
    except Exception:
        raw = b""
    raw = raw[:max_bytes]
    truncated = size_raw > max_bytes or len(raw) < min(size_raw, max_bytes)
    return {"data": raw, "size": len(raw), "truncated": truncated}


_EXT_BY_CT = {
    "text/csv": ".csv",
    "application/json": ".json",
    "text/json": ".json",
    "text/plain": ".txt",
    "text/html": ".html",
    "application/xml": ".xml",
    "text/xml": ".xml",
    "application/zip": ".zip",
    "application/pdf": ".pdf",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.ms-excel": ".xls",
}


def evidence_filename(
    filename: str | None, content_type: str, *, prefix: str = "fetch"
) -> str:
    """证据文件名：优先模型给的 filename（清洗），否则按 content-type 推断扩展名。"""
    ct = (content_type or "").split(";")[0].strip().lower()
    base = os.path.basename(str(filename or "").strip())
    safe = re.sub(r"[^0-9A-Za-z._\-\u4e00-\u9fff]", "_", base)[:80].strip("._") or ""
    if safe:
        if "." not in safe:
            safe += _EXT_BY_CT.get(ct, ".bin")
        return safe
    return f"{prefix}_{int(time.time())}{_EXT_BY_CT.get(ct, '.bin')}"


def save_fetch_evidence_file(
    evidence_dir: str, ev: dict, filename: str | None, content_type: str
) -> dict:
    """把 evidence 字节写入证据目录；返回可并入 tool_args 的路径/大小字段。"""
    try:
        from core.run_evidence import web_path_for

        name = evidence_filename(filename, content_type)
        os.makedirs(evidence_dir, exist_ok=True)
        path = os.path.join(evidence_dir, name)
        with open(path, "wb") as fh:
            fh.write(ev.get("data") or b"")
        return {
            "evidence_path": web_path_for(path),
            "evidence_size": ev.get("size"),
            "evidence_truncated": bool(ev.get("truncated")),
        }
    except Exception as exc:  # 落盘失败不改变请求判定（如实标注）
        return {"evidence_error": f"证据落盘失败：{exc}"}


async def run_fetch_action(call_tool, value: str, *, max_bytes: int | None = None) -> dict:
    """执行一次 fetch 动作（服务端/客户端共用编排）。

    ``call_tool(tool_name, args) -> {"success": bool, "text": str, "error": str?}``

    返回 ``{"success","error","api","token","evidence"?,"filename"?}``；
    ``evidence`` 为 ``{"data": bytes,"size","truncated"}``（由调用方落盘）。
    """
    spec = validate_fetch_value(value)
    token = uuid.uuid4().hex[:16]
    res = await call_tool("browser_evaluate", {"function": build_fetch_js(spec, token)})
    if not res.get("success"):
        return {
            "success": False,
            "error": res.get("error") or res.get("text") or "fetch 执行失败",
            "api": None,
            "token": token,
        }
    try:
        data = parse_fetch_result(res.get("text") or "")
    except ValueError as exc:
        return {"success": False, "error": str(exc), "api": None, "token": token}

    status = data.get("status")
    api = {
        "status": status,
        "size": data.get("size"),
        "content_type": data.get("content_type") or "",
        "binary": bool(data.get("binary")),
        "excerpt": (data.get("excerpt") or "")[:EXCERPT_BYTES],
        "method": spec["method"],
        "url": spec["url"],
    }
    out: dict = {"success": True, "error": None, "api": api, "token": token}

    if data.get("error"):
        out["success"] = False
        out["error"] = f"页面内请求失败：{data['error']}"

    if spec["evidence"] and data.get("stored") is not None:
        try:
            out["evidence"] = await _collect_evidence(
                call_tool, token, data, max_bytes or resolve_max_bytes()
            )
            out["filename"] = spec["filename"]
        except Exception as exc:  # 回传失败不改变请求判定（如实标注）
            out["evidence_error"] = str(exc)
            await _release(call_tool, token)

    if out["success"] and not (
        isinstance(status, int) and 200 <= status < 400
    ):
        out["success"] = False
        out["error"] = f"页面 API 返回 {status}"
    return out


__all__ = [
    "FETCH_METHODS",
    "CHUNK_CHARS",
    "EXCERPT_BYTES",
    "validate_fetch_value",
    "build_fetch_js",
    "build_fetch_info_js",
    "build_fetch_chunk_js",
    "build_fetch_release_js",
    "parse_fetch_result",
    "run_fetch_action",
    "resolve_max_bytes",
    "evidence_filename",
    "save_fetch_evidence_file",
]
