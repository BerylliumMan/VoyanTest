# app/services/api_ai.py — 接口测试 AI 辅助（031 US11）
#
# 契约（spec FR-020、contracts §1.5）：
#   · 从**响应样本**生成候选断言/提取器：LLM 输出 → **服务端 schema 校验** → 合法候选返回、非法候选丢弃并给原因
#   · LLM 不可用/输出不可解析 → 抛 ApiAiUnavailable，端点转 503（前端降级隐藏）
#   · 覆盖率双口径：被引用（用例 api_spec 引用 definition_id）与最近执行（最近一次 run 的终态）——
#     两个信号都要给出，避免「引用了但从没通过」被算作已覆盖
from __future__ import annotations

import json
import logging
from typing import Any, Optional

from core.api_spec import ALLOWED_ASSERTION_CONDITIONS, ALLOWED_ASSERTION_TYPES

logger = logging.getLogger(__name__)


class ApiAiUnavailable(RuntimeError):
    """LLM 不可用或输出不可用（端点转 503）。"""


SUGGEST_PROMPT = """你是接口测试断言助手。用户给你一次真实的 HTTP 响应样本，请给出**可判定**的断言候选。

硬规则：
1. 只输出 JSON 数组，不要 Markdown、不要解释。每项形如
   {"type": "status_code", "condition": "equals", "expected": 200, "name": "状态码 200"}
2. type 只能取：status_code / header / jsonpath / jsonschema / regex / response_time / body_contains / expression
3. 值必须**具体**（真实出现的状态码、字段路径与期望值）；禁止「应正确」「不为空」这类无法判定的描述
4. jsonpath 断言请给出真实存在的路径（用 $. 开头）；expression 断言结果必须为布尔
5. 最多 6 条，按价值从高到低排序；宁少勿滥
"""


def _rule_check(item: dict) -> Optional[str]:
    """单条候选的规则校验；返回 None 表示合法，否则返回原因。"""
    if not isinstance(item, dict):
        return "候选不是对象"
    atype = str(item.get("type") or "")
    if atype not in ALLOWED_ASSERTION_TYPES:
        return f"type 非法: {atype!r}"
    condition = str(item.get("condition") or ("expression" if atype == "expression" else "equals"))
    if condition not in ALLOWED_ASSERTION_CONDITIONS:
        return f"condition 非法: {condition!r}"
    if atype == "expression":
        if not str(item.get("expression") or "").strip():
            return "表达式断言缺少 expression"
        return None
    expected = item.get("expected")
    if atype == "jsonpath":
        if not str(item.get("expression") or "").strip():
            return "jsonpath 断言缺少 expression（路径）"
        if condition not in ("exists", "not_exists") and expected in (None, ""):
            return "jsonpath 断言缺少 expected"
        return None
    if expected in (None, ""):
        return "缺少 expected"
    return None


def validate_suggestions(raw_items: Any) -> tuple[list[dict], list[dict]]:
    """LLM 候选 → (合法列表, 丢弃列表[{item, reason}])。

    合法项做归一化：null 值剔除、字符串裁剪、补 name（便于前端展示）。
    """
    valid: list[dict] = []
    dropped: list[dict] = []
    seen: set[tuple] = set()
    for item in raw_items if isinstance(raw_items, list) else []:
        reason = _rule_check(item)
        if reason:
            dropped.append({"item": item if isinstance(item, dict) else str(item), "reason": reason})
            continue
        atype = str(item.get("type"))
        normalized = {
            "type": atype,
            "condition": str(item.get("condition") or ("expression" if atype == "expression" else "equals")),
            "expression": str(item.get("expression") or ""),
            "expected": item.get("expected") if atype != "expression" else None,
            "name": str(item.get("name") or f"{atype} 断言"),
            "enable": True,
        }
        key = (normalized["type"], normalized["condition"], normalized["expression"], str(normalized["expected"]))
        if key in seen:
            dropped.append({"item": item, "reason": "与已有候选重复"})
            continue
        seen.add(key)
        valid.append(normalized)
    return valid, dropped


def _extract_json_array(text: str) -> list:
    """从模型输出里取第一个 JSON 数组（容错 Markdown 包裹/前后散文）。"""
    raw = str(text or "")
    start = raw.find("[")
    end = raw.rfind("]")
    if start == -1 or end == -1 or end <= start:
        return []
    body = raw[start : end + 1]
    try:
        data = json.loads(body)
        return data if isinstance(data, list) else []
    except (ValueError, TypeError):
        return []


async def suggest_assertions(sample: dict[str, Any], *, agent_type: str = "generation") -> dict[str, Any]:
    """根据响应样本生成候选断言。

    ``sample``：{method, url, status_code, headers, body_text}
    """
    from app.gen.model_client import call_model

    method = str(sample.get("method") or "GET")
    url = str(sample.get("url") or "")
    status_code = sample.get("status_code")
    headers = sample.get("headers") or {}
    body_text = str(sample.get("body_text") or "")[:4000]
    user_content = (
        f"请求：{method} {url}\n"
        f"响应状态码：{status_code}\n"
        f"响应头（部分）：{json.dumps({k: str(v)[:120] for k, v in list(headers.items())[:12]}, ensure_ascii=False)}\n"
        f"响应体（截断 4000 字符）：\n{body_text}"
    )
    try:
        text = await call_model(
            [
                {"role": "system", "content": SUGGEST_PROMPT},
                {"role": "user", "content": user_content},
            ],
            agent_type=agent_type,
        )
    except Exception as exc:  # noqa: BLE001 - 统一转 503 语义
        logger.warning("AI 断言建议调用失败: %s", exc, exc_info=True)
        raise ApiAiUnavailable(f"AI 服务不可用：{type(exc).__name__}") from exc

    items = _extract_json_array(text)
    if not items:
        raise ApiAiUnavailable("AI 未返回可解析的断言候选（输出格式不符）")
    valid, dropped = validate_suggestions(items)
    if not valid:
        raise ApiAiUnavailable(f"AI 候选均不合法（丢弃 {len(dropped)} 条）")
    return {"suggestions": valid[:6], "dropped": dropped[:6]}


# ── 覆盖率双口径 ───────────────────────────────────────────────────────────


def build_coverage_items(
    definitions: list[dict],
    references: dict[int, int],
    last_runs: dict[int, dict],
) -> tuple[list[dict], list[dict]]:
    """双口径覆盖明细与缺口清单（纯函数，便于测试）。

    - ``references``：{definition_id: 引用它的用例数}
    - ``last_runs``：{definition_id: {"status": …, "run_at": …}}（该定义相关用例的最近一次执行）
    返回 (明细列表, 缺口清单)。缺口 = 未被引用 或 最近执行非通过。
    """
    items: list[dict] = []
    gaps: list[dict] = []
    for definition in definitions:
        def_id = int(definition.get("id"))
        ref_count = int(references.get(def_id, 0))
        last = last_runs.get(def_id) or {}
        status = last.get("status")
        referenced = ref_count > 0
        item = {
            "definition_id": def_id,
            "name": definition.get("name") or "",
            "method": definition.get("method") or "",
            "path": definition.get("path") or "",
            "referenced": referenced,
            "reference_count": ref_count,
            "last_status": status,
            "last_run_at": last.get("run_at"),
        }
        items.append(item)
        if not referenced:
            gaps.append({"definition_id": def_id, "path": item["path"], "reason": "未被任何用例引用"})
        elif status != "passed":
            gaps.append(
                {
                    "definition_id": def_id,
                    "path": item["path"],
                    "reason": f"最近执行未通过（{status or '从未执行'}）",
                }
            )
    return items, gaps


__all__ = [
    "ApiAiUnavailable",
    "SUGGEST_PROMPT",
    "build_coverage_items",
    "suggest_assertions",
    "validate_suggestions",
]
