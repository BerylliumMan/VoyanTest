"""B2 运行证据：网络请求证据 + 视频证据（与 032 console 证据同一模式）。

- 纯函数为主，两条执行链路（客户端 AgentBridge / 服务端 AgentRunner）与
  探索链（core/chat/browse.py）共用同一口径；
- 所有失败路径静默降级，不阻塞执行、不引入新异常面。

契约（@playwright/mcp 0.0.80/0.0.83 实测，2026-10-15）：
- browser_network_requests {"static": false} 输出行形如:
    `3. [GET] http://x/fail.png => [FAILED] net::ERR_UNSAFE_PORT`
    `4. [GET] http://x/api => [200] OK`
    末尾有一行 `Note: N static requests not shown, ...`（跳过）
- browser_start_video {"filename": "x.webm"} → 文件写入 MCP 输出目录（进程 CWD），
  browser_stop_video 结果文本含 `- [Video](./x.webm)`（相对输出目录）。
"""

from __future__ import annotations

import base64
import logging
import os
import re
from typing import Any, Optional

logger = logging.getLogger(__name__)

MAX_ENTRY_CHARS = 300
MAX_VIDEO_B64_BYTES = 8 * 1024 * 1024  # 客户端回传上限（8MB）

_SKIP_PREFIXES = ("###", "note:", "total", "returning 0")

# 请求行: `N. [METHOD] url => [STATUS] ...`
_REQ_LINE_RE = re.compile(r"^\d+\.\s+\[[A-Z]+\]\s+\S+.*=>\s*\[([^\]]+)\]")
_OK_STATUS_RE = re.compile(r"^[123]\d\d$")


def network_problem_lines(
    text: str | None, max_entries: int = 10, max_len: int = MAX_ENTRY_CHARS,
) -> list[str]:
    """从 browser_network_requests 输出提取问题请求（FAILED / 4xx / 5xx）。"""
    out: list[str] = []
    for raw in str(text or "").splitlines():
        s = raw.strip()
        if not s or s.lower().startswith(_SKIP_PREFIXES):
            continue
        # 浏览器通用噪声（favicon 404）：不作为网络证据
        if "favicon.ico" in s:
            continue
        m = _REQ_LINE_RE.match(s)
        if not m:
            continue
        status = m.group(1).strip()
        if status.upper() == "FAILED" or not _OK_STATUS_RE.match(status):
            out.append(s[:max_len])
    # 去重（保持顺序）后按上限截断
    seen: set[str] = set()
    kept: list[str] = []
    for e in out:
        if e in seen:
            continue
        seen.add(e)
        kept.append(e)
    if max_entries > 0 and len(kept) > max_entries:
        kept = kept[:max_entries]
    return kept


def apply_network_evidence(
    logs: list[dict], entries: list[str], max_entries: int = 10,
) -> list[dict]:
    """把网络问题条目合并进步骤日志（镜像 console 证据的合并式归属）。"""
    kept = [e for e in (entries or []) if e]
    if max_entries > 0 and len(kept) > max_entries:
        kept = kept[:max_entries]
    if not kept:
        return logs
    summary = "; ".join(kept)
    for row in reversed(logs):
        if str(row.get("level", "")).lower() in ("error", "critical"):
            row["message"] = f"{row.get('message') or ''} | [network] {summary}"
            return logs
    logs.append({
        "step_id": None,
        "level": "warning",
        "message": f"[network] {summary}",
        "screenshot_path": None,
    })
    return logs


def apply_video_evidence(logs: list[dict], web_path: str | None) -> list[dict]:
    """视频证据落为一条运行级「信息」行（可点击路径在报告页高亮）。"""
    if not web_path:
        return logs
    logs.append({
        "step_id": None,
        "level": "warning",
        "message": f"[video] 视频证据: {web_path}",
        "screenshot_path": None,
    })
    return logs


_VIDEO_NAME_RE = re.compile(r"\[Video\]\(\.?/?([^)\s]+\.webm)\)", re.IGNORECASE)
_WEBM_TOKEN_RE = re.compile(r"([\w\-.]+\.webm)", re.IGNORECASE)


def parse_video_filename(text: str | None) -> Optional[str]:
    """从 browser_stop_video 结果文本解析视频文件名（容错）。"""
    s = str(text or "")
    m = _VIDEO_NAME_RE.search(s)
    if m:
        return os.path.basename(m.group(1))
    m = _WEBM_TOKEN_RE.search(s)
    return m.group(1) if m else None


def find_newest_video(output_dir: str, prefix: str | None = None) -> Optional[str]:
    """在输出目录（非递归）找最新 .webm（可按前缀过滤）。找不到返回 None。"""
    try:
        names = [
            os.path.join(output_dir, n)
            for n in os.listdir(output_dir)
            if n.lower().endswith(".webm") and (not prefix or n.startswith(prefix))
        ]
    except OSError:
        return None
    if not names:
        return None
    names.sort(key=lambda p: os.path.getmtime(p) if os.path.exists(p) else 0)
    return names[-1]


def save_b64_file(b64_data: str | None, dest_path: str, max_bytes: int = MAX_VIDEO_B64_BYTES) -> Optional[str]:
    """客户端回传的 base64 → 落盘；超限/异常返回 None（不抛）。"""
    if not b64_data:
        return None
    try:
        if "," in str(b64_data):
            b64_data = str(b64_data).split(",", 1)[1]
        raw = base64.b64decode(b64_data)
        if not raw or len(raw) > max_bytes:
            logger.warning("证据文件超限未落盘: %s bytes", len(raw))
            return None
        os.makedirs(os.path.dirname(dest_path) or ".", exist_ok=True)
        with open(dest_path, "wb") as fh:
            fh.write(raw)
        return dest_path
    except Exception:  # noqa: BLE001 - 证据落盘失败不影响执行
        logger.debug("证据文件落盘失败", exc_info=True)
        return None


def read_video_b64(path: str, max_bytes: int = MAX_VIDEO_B64_BYTES) -> Optional[str]:
    """读取本地视频 → base64（客户端回传用）；超限/异常返回 None。"""
    try:
        if not path or not os.path.isfile(path):
            return None
        size = os.path.getsize(path)
        if size <= 0 or size > max_bytes:
            return None
        with open(path, "rb") as fh:
            return base64.b64encode(fh.read()).decode()
    except Exception:  # noqa: BLE001
        return None


def web_path_for(abs_path: str | None, root: str = "reports") -> Optional[str]:
    """容器内绝对/相对路径 → 前端可访问的 /reports 相对路径（web 别名恒为 reports/）。"""
    if not abs_path:
        return None
    try:
        rel = os.path.relpath(os.path.abspath(abs_path), os.path.abspath(root))
        if rel.startswith(".."):
            return None
        return "reports/" + rel.replace(os.sep, "/")
    except ValueError:
        return None


async def start_video_via_caller(caller, filename: str, *, timeout: float = 20.0) -> bool:
    """通过统一 caller(tool, args) 启动视频录制（服务端 MCP / 客户端 act 通道均可）。"""
    import asyncio

    try:
        r = await asyncio.wait_for(
            caller("browser_start_video", {"filename": filename}), timeout=timeout,
        )
    except Exception:
        return False
    return bool(isinstance(r, dict) and r.get("success", True) and not r.get("error"))


async def stop_video_via_caller(caller, *, timeout: float = 30.0) -> Optional[str]:
    """停止视频并返回结果文本（供解析文件名）；失败返回 None。"""
    import asyncio

    try:
        r = await asyncio.wait_for(caller("browser_stop_video", {}), timeout=timeout)
    except Exception:
        return None
    if not isinstance(r, dict) or not (r.get("success", True)):
        return None
    return str(r.get("text") or r.get("result") or "")


def network_request_args() -> dict[str, Any]:
    """browser_network_requests 的标准证据参数（非静态；由引擎侧调用）。"""
    return {"static": False}
