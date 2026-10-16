"""036 US3：诊断脚本受控 SDK（http / db / platform / files）。

设计（contracts C4）：
- 沙箱注入命名空间；脚本在沙箱线程内同步执行，异步能力经 ``run_async`` 桥回主事件循环
  （生产 = asyncio.run_coroutine_threadsafe；禁止在线程池里 asyncio.run 新事件循环）；
- db：只读强制（剥注释/字面量 → 仅 SELECT/WITH/EXPLAIN、禁多语句与写关键字）+
  敏感表 denylist + 行上限 200 + 独立会话（不复用请求会话）；
- http：单次超时 ≤10s、响应读 ≤2MB 截断标注、SSRF 无限制（平台口径）；
- platform：包装 app.services.tool_queries（身份绑定 user_id，双层权限在服务层强制）；
- files：仅 data/、reports/（realpath + commonpath 白名单）、读 ≤1MB、二进制拒绝；
- 计数（http_calls/db_queries）入审计 details。
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

MAX_HTTP_TIMEOUT_S = 10.0
MAX_HTTP_BYTES = 2 * 1024 * 1024
DB_ROW_LIMIT = 200
MAX_FILE_BYTES = 1024 * 1024
ALLOWED_FILE_ROOTS = ("data", "reports")

_SENSITIVE_TABLES = (
    "users", "sessions", "api_tokens", "audit_logs", "ai_configs",
)
_WRITE_KEYWORDS_RE = re.compile(
    r"\b(insert|update|delete|drop|alter|create|truncate|grant|revoke|copy|vacuum|into|returning)\b",
    re.I,
)
_COMMENT_BLOCK_RE = re.compile(r"/\*.*?\*/", re.S)
_COMMENT_LINE_RE = re.compile(r"--[^\n]*")
_STRING_LITERAL_RE = re.compile(r"'(?:[^'\\]|\\.)*'")


class SdkState:
    """一次脚本执行的调用计数（入审计）。"""

    def __init__(self) -> None:
        self.http_calls = 0
        self.db_queries = 0


# ── SQL 只读校验 ──────────────────────────────────────────────────────────


def _strip_sql_noise(sql: str) -> str:
    """剥离注释与字符串字面量（校验用；保留结构关键字）。"""
    s = _COMMENT_BLOCK_RE.sub(" ", sql or "")
    s = _COMMENT_LINE_RE.sub(" ", s)
    s = _STRING_LITERAL_RE.sub("''", s)
    return s


def check_readonly_sql(sql: str) -> str:
    """返回规范化后的 SQL；不合法抛 ValueError（可读原因）。"""
    raw = (sql or "").strip()
    if not raw:
        raise ValueError("SQL 不能为空")
    cleaned = _strip_sql_noise(raw).strip()
    # 多语句：分号后仍有内容（末尾分号允许）
    body = cleaned.rstrip(";").strip()
    if ";" in body:
        raise ValueError("仅允许单条只读语句（检测到多语句）")
    head = body.lstrip("(").strip().split(None, 1)
    first = head[0].upper() if head else ""
    if first not in ("SELECT", "WITH", "EXPLAIN"):
        raise ValueError("仅允许只读查询（SELECT/WITH/EXPLAIN）")
    if _WRITE_KEYWORDS_RE.search(body):
        raise ValueError("查询包含写关键字，已拒绝（只读诊断口径）")
    low = body.lower()
    for table in _SENSITIVE_TABLES:
        if re.search(rf"\b{table}\b", low):
            raise ValueError(f"敏感表 {table} 不允许查询")
    return raw


async def _query_once(db_factory, sql: str, params: Optional[dict], limit: int) -> dict:
    async with db_factory() as session:
        try:
            from sqlalchemy import text

            await session.execute(text("SET TRANSACTION READ ONLY"))
        except Exception:
            # 部分驱动/环境不支持；只读由 SQL 校验兜底
            logger.debug("SET TRANSACTION READ ONLY 跳过", exc_info=True)
        from sqlalchemy import text

        res = await session.execute(text(sql), dict(params or {}))
        columns = list(res.keys())
        rows_raw = res.fetchmany(limit + 1)
    truncated = len(rows_raw) > limit
    rows = [list(r) for r in rows_raw[:limit]]
    return {"columns": columns, "rows": rows, "truncated": truncated}


# ── 命名空间实现 ──────────────────────────────────────────────────────────


class _DbNS:
    def __init__(self, run_async, db_factory, state: SdkState):
        self._run = run_async
        self._db_factory = db_factory
        self._state = state

    def query(self, sql: str, params: Optional[dict] = None, limit: int = DB_ROW_LIMIT) -> dict:
        normalized = check_readonly_sql(sql)
        try:
            limit = max(1, min(int(limit or DB_ROW_LIMIT), DB_ROW_LIMIT))
        except (TypeError, ValueError):
            limit = DB_ROW_LIMIT
        self._state.db_queries += 1
        return self._run(_query_once(self._db_factory, normalized, params, limit))


class _HttpNS:
    def __init__(self, state: SdkState, client_factory):
        self._state = state
        self._client_factory = client_factory

    def request(self, method: str, url: str, *, headers=None, json=None, data=None, timeout=None) -> dict:
        try:
            t = float(timeout) if timeout is not None else MAX_HTTP_TIMEOUT_S
        except (TypeError, ValueError):
            t = MAX_HTTP_TIMEOUT_S
        t = max(0.1, min(t, MAX_HTTP_TIMEOUT_S))
        self._state.http_calls += 1
        with self._client_factory(timeout=t, follow_redirects=True) as client:
            resp = client.request(method.upper(), url, headers=headers, json=json, data=data, timeout=t)
        content = resp.content or b""
        truncated = len(content) > MAX_HTTP_BYTES
        raw = content[:MAX_HTTP_BYTES]
        out: dict[str, Any] = {
            "status": getattr(resp, "status_code", None),
            "headers": dict(getattr(resp, "headers", {}) or {}),
            "text": raw.decode("utf-8", errors="replace"),
            "truncated": truncated,
        }
        try:
            out["json"] = resp.json()
        except Exception:
            out["json"] = None
        return out

    def get(self, url, **kw):
        return self.request("GET", url, **kw)

    def post(self, url, **kw):
        return self.request("POST", url, **kw)

    def put(self, url, **kw):
        return self.request("PUT", url, **kw)

    def patch(self, url, **kw):
        return self.request("PATCH", url, **kw)

    def delete(self, url, **kw):
        return self.request("DELETE", url, **kw)


class _PlatformNS:
    """包装 tool_queries（异步）→ 同步桥；身份 user_id 由执行器绑定。"""

    def __init__(self, run_async, db_factory, user_id):
        self._run = run_async
        self._db_factory = db_factory
        self._user_id = user_id

    async def _call(self, fn_name: str, **kwargs):
        from app.services import tool_queries

        fn = getattr(tool_queries, fn_name)
        async with self._db_factory() as session:
            res = await fn(session, self._user_id, **kwargs)
        return res.to_dict() if hasattr(res, "to_dict") else res

    async def _count_cases(self, **kwargs):
        from core.tools.builtin.demo import count_cases
        from core.tools.registry import ToolContext

        async with self._db_factory() as session:
            ctx = ToolContext(db=session, user_id=self._user_id, source="chat")
            res = await count_cases(ctx, **kwargs)
        return res.to_dict() if hasattr(res, "to_dict") else res

    def list_projects(self, **kw):
        return self._run(self._call("list_projects", **kw))

    def search_cases(self, **kw):
        return self._run(self._call("search_cases", **kw))

    def get_case(self, **kw):
        return self._run(self._call("get_case", **kw))

    def list_suites(self, **kw):
        return self._run(self._call("list_suites", **kw))

    def list_batches(self, **kw):
        return self._run(self._call("list_batches", **kw))

    def get_batch(self, **kw):
        return self._run(self._call("get_batch", **kw))

    def count_cases(self, **kw):
        return self._run(self._count_cases(**kw))


class _FilesNS:
    def __init__(self, state: SdkState):
        self._state = state

    @staticmethod
    def _resolve(path: str, *, need_dir: bool = False) -> tuple[str, str]:
        rel = str(path or "").strip().lstrip("/").replace("\\", "/")
        if not rel:
            rel = "."
        base = os.path.realpath(os.getcwd())
        candidate = os.path.realpath(os.path.join(base, rel))
        allowed = False
        for root in ALLOWED_FILE_ROOTS:
            root_abs = os.path.realpath(os.path.join(base, root))
            try:
                if os.path.commonpath([candidate, root_abs]) == root_abs:
                    allowed = True
                    break
            except ValueError:
                continue
        if not allowed:
            roots_txt = "、".join(f"{r}/" for r in ALLOWED_FILE_ROOTS)
            raise ValueError(f"仅允许访问 {roots_txt} 目录下的文件")
        if not os.path.exists(candidate):
            raise ValueError(f"路径不存在：{rel}")
        if need_dir and not os.path.isdir(candidate):
            raise ValueError(f"不是目录：{rel}")
        return candidate, rel

    def read(self, path: str, max_bytes: int = MAX_FILE_BYTES) -> dict:
        try:
            cap = max(1, min(int(max_bytes or MAX_FILE_BYTES), MAX_FILE_BYTES))
        except (TypeError, ValueError):
            cap = MAX_FILE_BYTES
        candidate, rel = self._resolve(path)
        if os.path.isdir(candidate):
            raise ValueError("目标是目录，请用 files.list")
        with open(candidate, "rb") as fh:
            head = fh.read(min(cap + 1, 4096))
            if b"\x00" in head:
                raise ValueError("二进制文件不支持读取（仅文本）")
            data = head + fh.read(max(0, cap + 1 - len(head)))
        truncated = len(data) > cap
        return {
            "path": rel,
            "text": data[:cap].decode("utf-8", errors="replace"),
            "truncated": truncated,
        }

    def list(self, path: str = ".") -> list:
        candidate, rel = self._resolve(path or ".", need_dir=True)
        out = []
        for name in sorted(os.listdir(candidate)):
            full = os.path.join(candidate, name)
            try:
                size = os.path.getsize(full) if os.path.isfile(full) else None
            except OSError:
                size = None
            out.append({
                "name": name,
                "type": "dir" if os.path.isdir(full) else "file",
                "size": size,
            })
        return out


def build_sdk(
    *,
    run_async: Callable[[Any], Any],
    db_factory: Callable[[], Any],
    user_id: Optional[int],
    state: SdkState,
    http_client_factory: Optional[Callable] = None,
) -> dict[str, Any]:
    """构建注入沙箱的 SDK 命名空间字典。

    ``run_async``：把协程桥到主事件循环的同步等待器（生产=run_coroutine_threadsafe）。
    ``db_factory``：返回异步上下文管理器（AsyncSession）的工厂。
    """
    if http_client_factory is None:
        import httpx

        http_client_factory = httpx.Client
    return {
        "http": _HttpNS(state, http_client_factory),
        "db": _DbNS(run_async, db_factory, state),
        "platform": _PlatformNS(run_async, db_factory, user_id),
        "files": _FilesNS(state),
    }


__all__ = ["SdkState", "build_sdk", "check_readonly_sql"]
