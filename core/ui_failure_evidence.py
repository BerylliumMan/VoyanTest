# core/ui_failure_evidence.py
# 041 US1：UI/客户端失败运行的确定性证据塑形（纯函数）。
#
# UI/客户端运行不写 report.json（report_path=None），证据来自：
#   - RunLog：用例步骤行 + ` | [console]/[network]/[video]` 合并段
#   - AgentToolCall：turn/tool/success/error/tool_args
#   - 失败截图路径（RunLog.screenshot_path 或显式传入）
# 本模块只做事实提取、截断与脱敏，不做归因判断。
from __future__ import annotations

import json
import re
from typing import Any, Iterable

from core.api_runner.variables import mask_secrets_in_text

MAX_TEXT = 800
MAX_LOG_LINES = 120
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_ERROR_LEVELS = {"error", "critical", "failed", "failure"}
_INFO_LEVELS = {"info", "success", "passed", "warning", "warn", "debug"}
_TAG_RE = re.compile(r"^\s*\[(console|network|video)\]\s*", re.I)


def truncate(value: Any, limit: int = MAX_TEXT) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    text = _ANSI_RE.sub("", text or "").strip()
    if len(text) > limit:
        return text[:limit] + f"\n...[TRUNCATED {len(text) - limit} chars]"
    return text


def mask_text(text: str) -> str:
    out = mask_secrets_in_text(text or "", [])
    out = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._\-]+", r"\1***", out)
    out = re.sub(
        r"(?i)\b(password|passwd|token|secret|api[_-]?key|authorization|cookie)\b"
        r"(\s*[=:]\s*)(\"?)[^\"\s,}]+",
        r"\1\2\3***",
        out,
    )
    return out


def split_segments(message: str) -> list[str]:
    return [seg.strip() for seg in (message or "").split(" | ") if seg.strip()]


def _level_ok(level: Any) -> bool | None:
    lv = str(level or "").strip().lower()
    if lv in _INFO_LEVELS:
        return True
    if lv in _ERROR_LEVELS:
        return False
    return None


def _log_rows(logs: Iterable[Any]) -> list[dict]:
    rows: list[dict] = []
    for lg in logs or []:
        if isinstance(lg, dict):
            level = lg.get("level")
            message = lg.get("message")
            screenshot = lg.get("screenshot_path")
        else:
            level = getattr(lg, "level", None)
            message = getattr(lg, "message", None)
            screenshot = getattr(lg, "screenshot_path", None)
        rows.append({
            "level": level,
            "message": truncate(message or "", 1200),
            "screenshot_path": screenshot,
        })
    return rows[:MAX_LOG_LINES]


def _tool_rows(tool_calls: Iterable[Any]) -> list[dict]:
    rows: list[dict] = []
    for tc in tool_calls or []:
        if isinstance(tc, dict):
            get = tc.get
        else:
            get = lambda k, d=None: getattr(tc, k, d)  # noqa: E731
        rows.append({
            "turn": get("turn_number", None),
            "tool": get("tool_name", None),
            "success": bool(get("success", 1)),
            "error": truncate(get("error_message", "") or "", 600),
            "args": _mask_args(get("tool_args", {}) or {}),
        })
    return rows[-40:]


def _mask_args(args: Any) -> dict:
    if not isinstance(args, dict):
        return {}
    out = {}
    for key, value in args.items():
        if isinstance(value, (dict, list)):
            out[key] = mask_text(truncate(value, 500))
        elif str(key).lower() in {
            "password", "passwd", "secret", "token", "authorization",
            "cookie", "api_key", "apikey",
        }:
            out[key] = "******"
        else:
            out[key] = mask_text(truncate(value, 300))
    return out


def build_ui_failure_evidence(
    *,
    run: Any,
    logs: Iterable[Any],
    tool_calls: Iterable[Any] | None = None,
    case_name: str | None = None,
    environment: str | None = None,
    screenshots: Iterable[str] | None = None,
    last_passed: dict | None = None,
) -> dict:
    """构造 UI 失败证据；返回结构化 dict，可直接 JSON 序列化。"""
    log_rows = _log_rows(logs)
    steps: list[dict] = []
    console: list[str] = []
    network: list[str] = []
    video: list[str] = []
    shot_paths: list[str] = []

    for idx, row in enumerate(log_rows, 1):
        level_ok = _level_ok(row["level"])
        if row.get("screenshot_path"):
            shot_paths.append(str(row["screenshot_path"]))
        for seg in split_segments(row["message"]):
            m = _TAG_RE.match(seg)
            tag = m.group(1).lower() if m else None
            clean = _TAG_RE.sub("", seg).strip()
            if tag == "console":
                console.append(mask_text(truncate(clean)))
                continue
            if tag == "network":
                network.append(mask_text(truncate(clean)))
                continue
            if tag == "video":
                video.append(mask_text(truncate(clean)))
                continue
            steps.append({
                "index": idx,
                "success": level_ok,
                "level": row["level"],
                "text": mask_text(truncate(clean)),
                "screenshot_path": row.get("screenshot_path"),
            })

    for path in screenshots or []:
        if path and str(path) not in shot_paths:
            shot_paths.append(str(path))

    failed = [s for s in steps if s["success"] is False]
    failed_step = (failed[-1] if failed else None)
    tools = _tool_rows(tool_calls)
    failed_tools = [t for t in tools if not t["success"]]

    missing: list[str] = []
    if not steps and not tools:
        missing.append("steps")
    if not console:
        missing.append("console")
    if not shot_paths:
        missing.append("screenshot")

    status = getattr(run, "status", None) if not isinstance(run, dict) else run.get("status")
    duration = getattr(run, "duration", None) if not isinstance(run, dict) else run.get("duration")
    mode = getattr(run, "execution_mode", None) if not isinstance(run, dict) else run.get("execution_mode")
    return {
        "run_id": getattr(run, "id", None) if not isinstance(run, dict) else run.get("id"),
        "case_id": getattr(run, "case_id", None) if not isinstance(run, dict) else run.get("case_id"),
        "case_name": mask_text(case_name or ""),
        "status": status,
        "duration_s": duration,
        "execution_mode": mode,
        "environment": mask_text(environment or "") or "未提供",
        "failed_step": (
            {
                "index": failed_step["index"],
                "text": failed_step["text"],
                "level": failed_step["level"],
            }
            if failed_step else None
        ),
        "steps": steps[-60:],
        "console": console[-40:],
        "network": network[-30:],
        "video": video[-10:],
        "screenshots": shot_paths[-10:],
        "tool_calls": tools,
        "failed_tool_calls": failed_tools[-10:],
        "last_passed": last_passed or None,
        "missing_evidence": missing,
    }
