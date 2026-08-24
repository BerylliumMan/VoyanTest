"""Agent 安全约束层：URL 白名单、Token 截断、幂等性检查。"""

import re
import logging
from typing import Any

logger = logging.getLogger(__name__)

# ── URL 白名单 ──────────────────────────────────────────────────────────────

# 默认只允许 http/https 协议，禁止 file:/// javascript:// etc
URL_SCHEMA_ALLOWLIST = {"http", "https"}
URL_MAX_LENGTH = 2048
ALLOWED_DOMAINS: list[str] | None = None  # None = 不限制域名


def set_allowed_domains(domains: list[str] | None) -> None:
    """设置允许的域名模式列表。例如 ['*.example.com', 'localhost']"""
    global ALLOWED_DOMAINS
    ALLOWED_DOMAINS = domains


def validate_url(url: str) -> tuple[bool, str]:
    """验证 URL 是否可访问。

    Returns:
        (True, "") 或 (False, 拒绝原因文本)
    """
    if not url or len(url) > URL_MAX_LENGTH:
        return False, f"URL 为空或超过长度限制 ({URL_MAX_LENGTH})"

    schema_match = re.match(r"^([a-zA-Z][a-zA-Z0-9+\-.]*):", url)
    if not schema_match:
        return False, "URL 缺少协议头"

    schema = schema_match.group(1).lower()
    if schema not in URL_SCHEMA_ALLOWLIST:
        return False, f"不允许的协议: {schema}（仅允许 {URL_SCHEMA_ALLOWLIST}）"

    if ALLOWED_DOMAINS is not None:
        from urllib.parse import urlparse
        parsed = urlparse(url)
        hostname = parsed.hostname or ""
        allowed = False
        for pattern in ALLOWED_DOMAINS:
            if pattern.startswith("*."):
                if hostname.endswith(pattern[1:]):
                    allowed = True
                    break
            elif hostname == pattern:
                allowed = True
                break
        if not allowed:
            return False, f"域名 {hostname} 不在白名单中"

    return True, ""


def validate_goto_action(action: dict[str, Any]) -> tuple[bool, str]:
    """验证 navigate/goto 操作的目标 URL。"""
    url = action.get("url") or action.get("value") or ""
    return validate_url(url)


# ── Token 截断 ──────────────────────────────────────────────────────────────

def _env_max_tokens() -> int:
    """025-ref-click: 上限从 SNAPSHOT_MAX_CHARS 对齐（chars≈tokens/4 反推），默认 30000 chars。"""
    import os
    try:
        return max(1000, int(os.getenv("SNAPSHOT_MAX_CHARS", "30000")) // 4)
    except (TypeError, ValueError):
        return 7500


MAX_SNAPSHOT_TOKENS = _env_max_tokens()  # 单个 snapshot 最大 token 数（近似，env 可调）
APPROX_CHARS_PER_TOKEN = 4  # 中英混合估算


def truncate_snapshot(snapshot: str, max_tokens: int = MAX_SNAPSHOT_TOKENS) -> str:
    """截断 DOM/AX Tree snapshot 到指定 token 预算内。

    025-ref-click: 优先使用 compress_snapshot 保交互 ref 行；
    此函数保留用于非快照文本的 head+tail 截断。
    """
    from core.snapshot_compress import compress_snapshot
    compressed = compress_snapshot(snapshot, max_chars=max_tokens * APPROX_CHARS_PER_TOKEN)
    if len(compressed) < len(snapshot):
        return compressed
    max_chars = max_tokens * APPROX_CHARS_PER_TOKEN
    if len(snapshot) <= max_chars:
        return snapshot

    # 保留开头 70% + 结尾 30%，中间用省略标记连接
    head_chars = int(max_chars * 0.7)
    tail_chars = int(max_chars * 0.3) - 20
    if tail_chars < 0:
        tail_chars = 0

    head = snapshot[:head_chars]
    tail = snapshot[-tail_chars:] if tail_chars > 0 else ""
    return f"{head}\n... [截断 {len(snapshot) - head_chars - tail_chars} 字符] ...\n{tail}"


def truncate_tool_args(args: dict, max_str_len: int = 500) -> dict:
    """截断工具参数中的长字符串值。"""
    result = {}
    for k, v in args.items():
        if isinstance(v, str) and len(v) > max_str_len:
            result[k] = v[:max_str_len] + f"...[+{len(v) - max_str_len} chars]"
        else:
            result[k] = v
    return result


# ── 幂等性检查 ──────────────────────────────────────────────────────────────

def make_run_key(case_id: int, batch_id: int | None = None) -> str:
    """生成幂等键。

    格式: run_{case_id}_{batch_id or 0}_{时间戳hash or uuid}
    """
    import uuid
    return f"run_{case_id}_{batch_id or 0}_{uuid.uuid4().hex[:12]}"
