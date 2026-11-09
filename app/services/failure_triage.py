"""049 失败分类与自愈：纯规则分类、批次汇总与通知判定。

设计约束（见 specs/049-failure-triage-self-heal/research.md）：
- 分类必须在毫秒级完成，不调用 LLM、不访问网络；
- 判定优先级：断言 > 定位 > 鉴权 > 数据 > 环境 > 脚本 > 未知；
- 写入 `failure_reason` 前统一脱敏并截断，避免把请求/响应原文落库。
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Iterable, Sequence

from core.ui_failure_evidence import mask_text, truncate

FAILURE_KINDS: tuple[str, ...] = (
    "env",
    "auth",
    "data",
    "assertion",
    "locator",
    "script",
    "unknown",
)

#: 允许自动重试的类别（可被配置覆盖，但断言/定位/脚本永远不在默认集合内）
RETRYABLE_KINDS: tuple[str, ...] = ("env",)

REASON_LIMIT = 500
REPORTS_ROOT = Path(os.path.abspath("reports")).resolve()

_ASSERTION_PATTERNS = (
    r"断言",
    r"\bassert(?:ion)?\b",
    r"json[\s_-]?schema",
    r"schema",
    r"字段缺失",
    r"期望",
    r"expected\b.*\bgot\b",
)
_LOCATOR_PATTERNS = (
    r"locator",
    r"selector",
    r"waiting for",
    r"元素",
    r"no element",
    r"element.*not (?:found|visible|attached)",
    r"strict mode violation",
    r"ref\b.*not found",
)
_AUTH_PATTERNS = (
    r"\b401\b",
    r"\b403\b",
    r"unauthori[sz]ed",
    r"forbidden",
    r"token",
    r"凭证",
    r"登录失效",
    r"鉴权",
)
_DATA_PATTERNS = (
    r"\b404\b",
    r"\b409\b",
    r"\b422\b",
    r"不存在",
    r"not found",
    r"no such",
    r"missing\b",
)
_ENV_PATTERNS = (
    r"timeout",
    r"timed out",
    r"connect",
    r"connection",
    r"unavailable",
    r"reset by peer",
    r"\bdns\b",
    r"name or service not known",
    r"\b50[0-9]\b",
    # 执行器真实文案（core/api_runner/runner.py::_http_error_label），
    # 异常类型名不一定带 timeout/connect 关键词，必须按中文标签兜底。
    r"连接失败",
    r"连接超时",
    r"请求超时",
    r"网络错误",
    r"连接被拒",
    r"服务不可用",
    r"超时",
)
_SCRIPT_PATTERNS = (
    r"未定义",
    r"undefined",
    r"\bnameerror\b",
    r"\bkeyerror\b",
    r"\btypeerror\b",
    r"变量",
    r"expression",
    r"数据集",
    r"invalid variable",
)

_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("assertion", _ASSERTION_PATTERNS),
    ("locator", _LOCATOR_PATTERNS),
    ("auth", _AUTH_PATTERNS),
    ("data", _DATA_PATTERNS),
    ("env", _ENV_PATTERNS),
    ("script", _SCRIPT_PATTERNS),
)


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(value)


def _iter_step_errors(steps: Sequence[Any] | None) -> Iterable[str]:
    for step in steps or ():
        if not isinstance(step, dict):
            continue
        if step.get("success") is False or str(step.get("status") or "") == "failed":
            text = _as_text(step.get("error")) or _as_text(step.get("message"))
            if text:
                yield text
            for assertion in step.get("assertions") or ():
                if isinstance(assertion, dict) and assertion.get("passed") is False:
                    yield _as_text(assertion.get("error")) or "断言失败"


def _has_failed_assertion(steps: Sequence[Any] | None) -> bool:
    for step in steps or ():
        if not isinstance(step, dict):
            continue
        for assertion in step.get("assertions") or ():
            if isinstance(assertion, dict) and assertion.get("passed") is False:
                return True
    return False


def _http_status(steps, text):
    """返回 ``(状态码, 是否来自真实响应)``。

    优先取**失败步骤**的真实响应状态（``step["response"]["status"]`` 或整数
    ``step["status"]``），其次才是错误文本里的数字——多步用例里登录步骤的 200
    不能覆盖目标接口的 502。
    """
    candidates = [s for s in (steps or ()) if isinstance(s, dict)]
    ordered = [s for s in candidates if s.get("success") is False]
    ordered += [s for s in candidates if s.get("success") is not False]

    for step in ordered:
        response = step.get("response")
        if isinstance(response, dict):
            try:
                code = int(response.get("status"))
            except (TypeError, ValueError):
                code = 0
            if 100 <= code < 600:
                return code, True
    for step in ordered:
        for key in ("status", "status_code", "http_status"):
            try:
                code = int(step.get(key))
            except (TypeError, ValueError):
                continue
            if 100 <= code < 600:
                return code, True
    match = re.search(r"\b([45]\d{2})\b", text or "")
    if match:
        return int(match.group(1)), False
    return None, False

def _match_kind(text: str) -> str | None:
    """按 ``_RULES`` 的顺序匹配错误文本，返回第一个命中的类别。"""
    haystack = (text or "").lower()
    if not haystack:
        return None
    for kind, patterns in _RULES:
        for pattern in patterns:
            if re.search(pattern, haystack):
                return kind
    return None


def _clean_reason(text: str) -> str:
    """脱敏 + 截断，写入 ``failure_reason`` 前统一处理。"""
    masked = mask_text(_as_text(text)).strip()
    return truncate(masked, REASON_LIMIT)


def classify_failure(
    *,
    error_text: str = "",
    steps=None,
    http_status: int | None = None,
) -> tuple[str, str]:
    """把一次失败归入 7 类之一，返回 ``(kind, reason)``。

    判定顺序（049 真机验证后修正）：

    1. **真实响应状态**优先：401/403 -> auth；5xx -> env；404/409/410/422 -> data。
       响应已经回来时，断言只是把"未授权/服务挂了"写成了失败文案，按状态归类更准确，
       否则过期 token 会被当成真实缺陷通知到人。
    2. 断言失败 -> assertion（只认 ``passed=false`` 的断言；通过的不算证据，
       否则所有带 jsonschema 断言的用例都会把环境故障误判成真实缺陷）。
    3. 其余按错误文本特征匹配：定位 -> 鉴权 -> 数据 -> 环境 -> 脚本。
    4. 都没有 -> unknown。
    """
    step_errors = list(_iter_step_errors(steps))
    text = "\n".join([_as_text(error_text), *step_errors]).strip()
    status, authoritative = _http_status(steps, text)
    if http_status is not None:
        status, authoritative = http_status, True

    if authoritative and status is not None:
        if status in (401, 403):
            return "auth", _clean_reason(text or f"HTTP {status}")
        if status >= 500:
            return "env", _clean_reason(text or f"HTTP {status}")
        if status in (404, 409, 410, 422):
            return "data", _clean_reason(text or f"HTTP {status}")

    if _has_failed_assertion(steps) or _match_kind(text) == "assertion":
        reason = next(
            (e for e in step_errors if re.search("|".join(_ASSERTION_PATTERNS), e.lower())),
            text or "断言未通过",
        )
        return "assertion", _clean_reason(reason)

    if status is not None:
        if status in (401, 403):
            return "auth", _clean_reason(text or f"HTTP {status}")
        if status >= 500:
            return "env", _clean_reason(text or f"HTTP {status}")
        if status in (404, 409, 410, 422):
            return "data", _clean_reason(text or f"HTTP {status}")

    kind = _match_kind(text)
    if kind is not None:
        reason = next((e for e in step_errors if _match_kind(e) == kind), text)
        return kind, _clean_reason(reason)

    return "unknown", _clean_reason(text or "无法判定失败原因")
def classify_api_result(result: dict[str, Any] | None) -> tuple[str, str]:
    """接口执行结果（``ApiRunResult`` 序列化后的 dict）→ 类别。"""
    payload = result or {}
    return classify_failure(
        error_text=_as_text(payload.get("error")),
        steps=payload.get("steps") or (),
    )


def classify_report(report: dict[str, Any] | None, *, error_text: str = "") -> tuple[str, str]:
    """报告 JSON（UI/接口共用）→ 类别。"""
    payload = report or {}
    return classify_failure(
        error_text=error_text or _as_text(payload.get("error")),
        steps=payload.get("steps") or (),
    )


def classify_logs(logs: Iterable[dict[str, Any]] | None) -> tuple[str, str]:
    """没有报告文件时的兜底：从 run_logs 的失败消息里判定。"""
    messages = [
        _as_text(entry.get("message"))
        for entry in logs or ()
        if isinstance(entry, dict)
        and str(entry.get("level") or "").upper() in ("ERROR", "CRITICAL", "FAILED")
    ]
    return classify_failure(error_text="\n".join(messages))


def load_report_steps(report_path: str | None) -> list[Any] | None:
    """安全读取报告 JSON 的 steps；路径越界或文件损坏返回 None。"""
    if not report_path:
        return None
    try:
        resolved = Path(os.path.abspath(report_path)).resolve()
        resolved.relative_to(REPORTS_ROOT)
    except (OSError, ValueError):
        return None
    try:
        with open(resolved, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    steps = data.get("steps") if isinstance(data, dict) else None
    return steps if isinstance(steps, list) else None


def classify_run(
    *,
    status: str,
    report_path: str | None = None,
    logs: Iterable[dict[str, Any]] | None = None,
) -> tuple[str, str] | None:
    """统一入口：失败返回 ``(kind, reason)``，非失败返回 None。"""
    if str(status or "") != "failed":
        return None
    steps = load_report_steps(report_path)
    if steps:
        return classify_report({"steps": steps})
    return classify_logs(logs)


def summarize_runs(runs: Iterable[Any]) -> dict[str, Any]:
    """汇总一批运行记录（ORM 或 dict）的失败类别。

    返回 ``{"failed": n, "kinds": {...}, "real_defects": n, "total": n, "passed": n}``。
    """
    total = passed = failed = 0
    kinds: dict[str, int] = {}
    for run in runs or ():
        status = _field(run, "status")
        total += 1
        if status == "passed":
            passed += 1
            continue
        if status != "failed":
            continue
        failed += 1
        kind = _field(run, "failure_kind") or "unknown"
        if kind not in FAILURE_KINDS:
            kind = "unknown"
        kinds[kind] = kinds.get(kind, 0) + 1
    return {
        "total": total,
        "passed": passed,
        "failed": failed,
        "kinds": kinds,
        "real_defects": failed - kinds.get("env", 0),
    }


def _field(item: Any, name: str) -> Any:
    if isinstance(item, dict):
        return item.get(name)
    return getattr(item, name, None)


def notify_verdict(
    summary: dict[str, Any],
    *,
    retry_batch_id: int | None = None,
    retried_cases: int = 0,
    auto_retry_enabled: bool = True,
    is_retry_batch: bool = False,
) -> tuple[str, str, str]:
    """根据分类汇总决定通知的 ``(type, title, message)``。

    :param is_retry_batch: 当前批次本身是自动重试批次。环境重试后仍然失败，
        说明不是瞬时抖动，需要升级为 ``error`` 叫人。
    """
    total = int(summary.get("total") or 0)
    passed = int(summary.get("passed") or 0)
    failed = int(summary.get("failed") or 0)
    kinds = dict(summary.get("kinds") or {})
    env = int(kinds.get("env") or 0)
    real = failed - env

    title = "批次运行完成"
    if failed <= 0:
        return "success", title, f"通过 {passed}/{total}，全部通过"

    counts = " / ".join(
        f"{label} {int(kinds.get(kind) or 0)}"
        for kind, label in (
            ("assertion", "真实缺陷"),
            ("locator", "定位"),
            ("data", "数据"),
            ("auth", "鉴权"),
            ("script", "脚本"),
            ("unknown", "未知"),
            ("env", "环境"),
        )
        if int(kinds.get(kind) or 0) > 0
    )

    if real > 0:
        return "error", title, f"失败 {failed}：{counts}"

    if is_retry_batch and env > 0:
        # 自动重试后仍只有环境失败：环境没有恢复，继续静默会掩盖故障。
        return "error", title, f"环境重试后仍失败 {failed}（环境 {env}）"

    if retry_batch_id and retried_cases > 0:
        return (
            "info",
            title,
            f"失败 {failed}（环境 {env}）· 已自动重试 {retried_cases} 条，批次 #{retry_batch_id}",
        )
    if not auto_retry_enabled:
        return "info", title, f"失败 {failed}（环境 {env}）· 可开启自动重试"
    return "info", title, f"失败 {failed}（环境 {env}）"


def decide_notification(
    summary: dict[str, Any],
    *,
    notify_real_only: bool = True,
    triage_enabled: bool = True,
    auto_retry_enabled: bool = True,
    retry_batch_id: int | None = None,
    retried_cases: int = 0,
    is_retry_batch: bool = False,
) -> tuple[str, str, str]:
    """通知分级入口：降噪关闭或分类关闭时回落到 048 之前的旧口径。

    旧口径：有失败即 ``error``，全过 ``success``，其余 ``info``。
    """
    if triage_enabled and notify_real_only:
        return notify_verdict(
            summary,
            retry_batch_id=retry_batch_id,
            retried_cases=retried_cases,
            auto_retry_enabled=auto_retry_enabled,
            is_retry_batch=is_retry_batch,
        )
    failed = int(summary.get("failed") or 0)
    passed = int(summary.get("passed") or 0)
    total = int(summary.get("total") or 0)
    ntype = "error" if failed > 0 else ("success" if passed else "info")
    return ntype, "批次运行完成", f"通过 {passed}/{total}，失败 {failed}"


__all__ = [
    "FAILURE_KINDS",
    "RETRYABLE_KINDS",
    "REASON_LIMIT",
    "classify_api_result",
    "classify_failure",
    "classify_logs",
    "classify_report",
    "classify_run",
    "decide_notification",
    "load_report_steps",
    "notify_verdict",
    "summarize_runs",
]
