# core/failure_signature.py
# 041 US3：失败签名与聚类（确定性纯函数；模型不参与分组）。
from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from typing import Any, Iterable

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_URL_RE = re.compile(r"https?://[^\s)\]]+", re.I)
_HEX_RE = re.compile(r"\b[0-9a-f]{8,}\b", re.I)
_UUID_RE = re.compile(r"\b[0-9a-f]{8}-[0-9a-f-]{20,}\b", re.I)
_NUM_RE = re.compile(r"\b\d+(?:\.\d+)?\b")
_TS_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}[T ][\d:.]+Z?\b")
_REF_RE = re.compile(r"\b(?:f\d+)?e\d+\b", re.I)
_SPACE_RE = re.compile(r"\s+")
_ENV_RE = re.compile(
    r"connection refused|connecterror|econnrefused|econnreset|timeout|timed out|"
    r"err_internet_disconnected|err_connection|dns|getaddrinfo|target closed|"
    r"browser has been closed|net::err|unreachable|no route to host",
    re.I,
)
_ELEMENT_RE = re.compile(r"ref .*not found|element.*not|strict mode|selector|locator|not visible|no element", re.I)
_ASSERT_RE = re.compile(r"assert|expected|期望|断言|to be|to contain|mismatch", re.I)


def clean(text: Any) -> str:
    return _SPACE_RE.sub(" ", _ANSI_RE.sub("", str(text or ""))).strip()


def normalize_error(text: Any, limit: int = 300) -> str:
    out = clean(text).lower()
    out = _UUID_RE.sub("<uuid>", out)
    out = _URL_RE.sub("<url>", out)
    out = _TS_RE.sub("<ts>", out)
    out = _REF_RE.sub("<ref>", out)
    out = _HEX_RE.sub("<hex>", out)
    out = _NUM_RE.sub("<n>", out)
    out = re.sub(r"\s+", " ", out).strip()
    return out[:limit]


def kind_hint(text: Any) -> str:
    raw = clean(text)
    if _ENV_RE.search(raw):
        return "environment"
    if _ASSERT_RE.search(raw):
        return "assertion"
    if _ELEMENT_RE.search(raw):
        return "element"
    return "unknown"


def _segments(message: str) -> list[str]:
    return [s.strip() for s in (message or "").split(" | ") if s.strip()]


def _log_dict(lg: Any) -> dict:
    if isinstance(lg, dict):
        return {
            "level": str(lg.get("level") or "").lower(),
            "message": clean(lg.get("message")),
            "screenshot": lg.get("screenshot_path"),
        }
    return {
        "level": str(getattr(lg, "level", "") or "").lower(),
        "message": clean(getattr(lg, "message", "")),
        "screenshot": getattr(lg, "screenshot_path", None),
    }


def build_signature(
    *,
    logs: Iterable[Any],
    tool_calls: Iterable[Any] | None = None,
) -> dict:
    """从 run_logs/tool_calls 提取稳定签名与可读摘要。"""
    failed_step = None
    error = ""
    console = ""
    for lg in (_log_dict(x) for x in logs or []):
        if failed_step is None and lg["level"] in ("error", "critical", "failed", "failure"):
            for seg in _segments(lg["message"]):
                if "[console]" in seg:
                    console = console or clean(seg)
                    continue
                failed_step = failed_step or clean(seg)
                error = error or clean(seg)
                break
        if not console:
            for seg in _segments(lg["message"]):
                if "[console]" in seg:
                    console = clean(seg)
                    break
    tool_error = ""
    for tc in tool_calls or []:
        get = tc.get if isinstance(tc, dict) else (lambda k, d=None, _tc=tc: getattr(_tc, k, d))
        if not bool(get("success", 1)):
            tool_error = tool_error or clean(get("error_message"))
    raw = " || ".join(x for x in [error, tool_error, console] if x)
    if not raw:
        raw = failed_step or "unknown"
    return {
        "failed_step": (failed_step or "")[:200],
        "error": normalize_error(raw),
        "raw_error": (error or tool_error or "")[:300],
        "console": console[:200],
        "kind_hint": kind_hint(raw),
        "signature": hashlib.sha256(normalize_error(raw).encode("utf-8")).hexdigest()[:32],
    }


def cluster_failures(
    entries: Iterable[dict],
    *,
    min_runs: int = 2,
) -> dict:
    """entries: [{run_id, case_id, signature, kind_hint, summary, started_at}]。"""
    groups: dict[str, dict] = {}
    singles: list[dict] = []
    total = 0
    for e in entries:
        total += 1
        sig = str(e.get("signature") or "")
        group = groups.get(sig)
        if group is None:
            group = {
                "signature": sig,
                "kind_hint": e.get("kind_hint") or "unknown",
                "summary": e.get("summary") or "",
                "run_count": 0,
                "case_ids": set(),
                "run_ids": [],
                "first_seen": e.get("started_at"),
                "last_seen": e.get("started_at"),
                "representative_run_id": e.get("run_id"),
            }
            groups[sig] = group
        group["run_count"] += 1
        group["run_ids"].append(e.get("run_id"))
        if e.get("case_id") is not None:
            group["case_ids"].add(e.get("case_id"))
        if e.get("started_at"):
            if not group["first_seen"] or str(e["started_at"]) < str(group["first_seen"]):
                group["first_seen"] = e["started_at"]
            if not group["last_seen"] or str(e["started_at"]) > str(group["last_seen"]):
                group["last_seen"] = e["started_at"]
        group["representative_run_id"] = e.get("run_id") or group["representative_run_id"]

    out_groups = []
    for sig, g in groups.items():
        if g["run_count"] >= min_runs:
            g = dict(g)
            g["case_ids"] = sorted(g["case_ids"])
            out_groups.append(g)
        else:
            singles.append({
                "run_id": g["representative_run_id"],
                "case_id": next(iter(g["case_ids"]), None),
                "signature": sig,
                "kind_hint": g["kind_hint"],
                "summary": g["summary"],
                "started_at": g["last_seen"],
            })
    out_groups.sort(key=lambda x: (-x["run_count"], str(x.get("last_seen") or "")))
    singles.sort(key=lambda x: str(x.get("started_at") or ""), reverse=True)
    return {
        "total_failures": total,
        "groups": out_groups,
        "singletons": singles,
    }
