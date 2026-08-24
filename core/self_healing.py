"""AI-powered locator healing for failed element location.

Prefer AX name/role candidates that re-bind to the current snapshot ref.
CSS selector healing remains a legacy fallback for non-AX paths.
"""

from __future__ import annotations

import asyncio
import json as _json
import logging
import os
from typing import Any, Optional

logger = logging.getLogger(__name__)

_cached_client = None


async def _get_cached_client():
    """Lazy-init 缓存的 AsyncOpenAI 客户端。"""
    global _cached_client
    if _cached_client is not None:
        return _cached_client
    try:
        from core.llm_wrapper import create_openai_client

        _cached_client = await create_openai_client()
    except (ValueError, RuntimeError) as exc:
        logger.warning("Failed to create LLM client: %s", exc, exc_info=True)
        return None
    return _cached_client


_AX_HEALING_PROMPT = """你是 Web 自动化测试专家。前一个步骤的元素定位失败了。
请分析当前页面的无障碍树（AX snapshot），给出最可能的目标控件 accessible name / role。

## 失败的步骤
- 描述: {step_description}
- 结构化: {structured_json}
- 错误: {error}

## 当前 AX snapshot
{dom_snapshot}

## 要求
只返回 JSON 数组（最多 3 个），按置信度降序：
[
  {{
    "name": "页面上真实可见的 accessible name（禁止省略号、禁止「按钮/图标」等类型词）",
    "role": "button|textbox|link|combobox|option|checkbox|menuitem|img|...",
    "confidence": 0.95,
    "reason": "为何是它"
  }}
]

若找不到相关元素，返回 []。不要返回 CSS/XPath。
"""


_HEALING_PROMPT = """你是 Web 自动化测试专家。前一个步骤的元素定位失败了。
请分析当前页面的 DOM 快照，找出最可能的目标元素，返回新的选择器。

## 失败的步骤
- 描述: {step_description}
- 原始选择器: {original_selector}
- 错误: {error}

## 当前页面 DOM 快照
{dom_snapshot}

## 要求
返回 JSON 数组，按置信度降序排列（最多 3 个候选）:
[
  {{
    "selector": "css选择器",
    "confidence": 0.95,
    "reason": "这个选择器匹配目标元素，因为..."
  }}
]

注意:
- 优先级: text=选择器 > CSS 选择器 > XPath
- 避免过于宽泛的选择器（如 div、span）
- 如果 DOM 中找不到任何相关元素，返回空数组 []
"""


async def _snapshot_text(mcp_manager) -> str:
    try:
        snapshot_result = await mcp_manager.call_tool("browser_snapshot", {})
        return snapshot_result.get("text", "") if snapshot_result.get("success") else ""
    except (RuntimeError, ConnectionError, OSError) as exc:
        logger.warning("Failed to get DOM snapshot for healing: %s", exc, exc_info=True)
        return ""


def _parse_llm_json_list(content: str) -> list:
    content = (content or "").strip()
    if content.startswith("```"):
        content = content.split("```")[1]
        if content.startswith("json"):
            content = content[4:]
    data = _json.loads(content)
    return data if isinstance(data, list) else []



def _compress(text: str) -> str:
    """025-ref-click: 智能压缩替代固定 8000 截断，保交互元素 ref 行。"""
    from core.snapshot_compress import compress_snapshot
    return compress_snapshot(text)


# MCP 在 ref 失效/幻觉时的错误特征（tab.ts: "Ref ... not found in the current page snapshot"）
_STALE_REF_MARKERS = (
    "not found in the current page snapshot",
    "ref not found",
)


def is_stale_ref_error(error: str | None) -> bool:
    """判定 act 失败是否为 ref 失效或幻觉 ref——两者共用刷新重试路径。"""
    if not error:
        return False
    e = str(error).lower()
    return any(marker in e for marker in _STALE_REF_MARKERS)


def build_failure_hint(error: str, candidates: list[dict] | None = None) -> str:
    """构造重试时附加给 LLM 的提示（含 heal 候选元素名）。"""
    hint = f"上一次操作失败: {error}。请基于最新快照重新选择目标元素，不要重复同一选择。"
    if candidates:
        names = " / ".join(
            f"{c.get('name')}({c.get('role') or 'element'})"
            for c in candidates[:3]
            if c.get("name")
        )
        if names:
            hint += f" 目标元素的可访问名称可能是: {names}。"
    return hint


async def recover_candidates(
    mcp_manager,
    step_description: str,
    error: str,
    structured_step: dict | None = None,
) -> list[dict]:
    """server-local 路径的 heal 候选获取；异常安全返回空列表。

    远程 Bridge 路径无 mcp_manager，调用方应跳过本函数仅用 hint 重试。
    """
    if mcp_manager is None:
        return []
    try:
        return await asyncio.wait_for(
            heal_ax_name_role(
                mcp_manager,
                step_description=step_description,
                error=error,
                structured_step=structured_step,
            ),
            timeout=30,
        )
    except Exception as exc:
        logger.warning("recover_candidates failed: %s", exc)
        return []


async def heal_ax_name_role(
    mcp_manager,
    step_description: str,
    error: str = "",
    structured_step: dict | None = None,
) -> list[dict[str, Any]]:
    """Ask LLM for accessible name/role candidates from the current AX tree."""
    dom_snapshot = await _snapshot_text(mcp_manager)
    if not dom_snapshot or len(dom_snapshot) < 10:
        return []

    prompt = _AX_HEALING_PROMPT.format(
        step_description=step_description,
        structured_json=_json.dumps(structured_step or {}, ensure_ascii=False)[:800],
        error=error,
        dom_snapshot=_compress(dom_snapshot),
    )
    try:
        client = await _get_cached_client()
        if client is None:
            logger.warning("LLM client unavailable for AX healing")
            return []
        response = await client.chat.completions.create(
            model=os.getenv("LLM_MODEL", "qwen-plus"),
            messages=[
                {"role": "system", "content": "你是 Web 自动化测试专家。只返回 JSON，无其他文字。"},
                {"role": "user", "content": prompt},
            ],
            temperature=0.1,
            max_tokens=1000,
        )
        content = response.choices[0].message.content or ""
        candidates = _parse_llm_json_list(content)
    except Exception as exc:  # noqa: BLE001 - heal LLM: swallow and return empty
        logger.warning("AX healing LLM failed: %s", exc, exc_info=True)
        return []

    valid: list[dict[str, Any]] = []
    for c in candidates:
        if not isinstance(c, dict):
            continue
        name = (c.get("name") or c.get("target_name") or "").strip()
        if not name:
            continue
        role = (c.get("role") or c.get("target_role") or "").strip().lower() or None
        valid.append({
            "name": name,
            "role": role,
            "confidence": float(c.get("confidence") or 0),
            "reason": str(c.get("reason") or ""),
        })
    return valid[:3]


async def try_heal_ax_rebind(
    mcp_manager,
    step_dict: dict,
    step_description: str,
    error: str = "",
    max_candidates: int = 3,
    healing_timeout: float = 10.0,
) -> Optional[dict[str, Any]]:
    """Produce name/role candidates and re-bind to a unique AX ref.

    Returns ``{"target_name", "target_role", "ref", "structured_step"}`` on success.
    """

    async def _do_heal():
        from core.step_intent import StepIntent, match_intent_candidates

        structured = step_dict.get("structured_step")
        if not isinstance(structured, dict):
            structured = {}
        action = (structured.get("action") or "click").strip().lower() or "click"

        candidates = await heal_ax_name_role(
            mcp_manager,
            step_description=step_description,
            error=error,
            structured_step=structured or None,
        )
        if not candidates:
            logger.info("Self-healing: LLM 未返回 name/role 候选")
            return None

        snap = await _snapshot_text(mcp_manager)
        if not snap:
            return None

        for cand in candidates[:max_candidates]:
            intent = StepIntent(
                action=action,
                target_role=cand.get("role"),
                target_name=cand["name"],
                value=structured.get("value"),
                thinking=f"self-heal: {cand.get('reason') or ''}",
            )
            matches = match_intent_candidates(
                snap,
                intent,
                frame_hint=structured.get("frame_hint"),
            )
            logger.info(
                "Self-healing AX: try name=%r role=%r matches=%s",
                cand["name"],
                cand.get("role"),
                len(matches),
            )
            if len(matches) != 1:
                # retry without role constraint
                if cand.get("role"):
                    intent2 = intent.model_copy(update={"target_role": None})
                    matches = match_intent_candidates(
                        snap,
                        intent2,
                        frame_hint=structured.get("frame_hint"),
                    )
            if len(matches) != 1:
                continue
            el = matches[0]
            new_struct = {
                **structured,
                "action": action,
                "target_name": el.get("name") or cand["name"],
            }
            if el.get("role") or cand.get("role"):
                new_struct["target_role"] = el.get("role") or cand.get("role")
            logger.info(
                "Self-healing: ✅ AX rebind name=%r role=%r ref=%s",
                new_struct["target_name"],
                new_struct.get("target_role"),
                el["ref"],
            )
            return {
                "target_name": new_struct["target_name"],
                "target_role": new_struct.get("target_role"),
                "ref": el["ref"],
                "structured_step": new_struct,
            }
        return None

    try:
        return await asyncio.wait_for(_do_heal(), timeout=healing_timeout)
    except asyncio.TimeoutError:
        logger.warning("Self-healing AX timed out after %ss", healing_timeout)
        return None


async def heal_selector(
    mcp_manager,
    original_selector: str,
    step_description: str,
    error: str = "",
) -> list[dict]:
    """Legacy: LLM CSS/text selector candidates (fallback only)."""
    dom_snapshot = await _snapshot_text(mcp_manager)
    if not dom_snapshot or len(dom_snapshot) < 10:
        return []

    prompt = _HEALING_PROMPT.format(
        step_description=step_description,
        original_selector=original_selector,
        error=error,
        dom_snapshot=_compress(dom_snapshot),
    )

    try:
        client = await _get_cached_client()
        if client is None:
            logger.warning("LLM client unavailable for healing")
            return []
        response = await client.chat.completions.create(
            model=os.getenv("LLM_MODEL", "qwen-plus"),
            messages=[
                {"role": "system", "content": "你是 Web 自动化测试专家。只返回 JSON，无其他文字。"},
                {"role": "user", "content": prompt},
            ],
            temperature=0.1,
            max_tokens=1000,
        )
        content = response.choices[0].message.content or ""
        candidates = _parse_llm_json_list(content)
    except Exception as exc:  # noqa: BLE001 - 自愈 LLM 调用：吞掉所有异常返回空候选
        logger.warning("LLM healing failed: %s", exc, exc_info=True)
        return []

    valid = []
    for c in candidates:
        if isinstance(c, dict) and "selector" in c:
            valid.append({
                "selector": c.get("selector", ""),
                "confidence": c.get("confidence", 0),
                "reason": c.get("reason", ""),
            })
    return valid[:3]


async def try_heal_and_retry(
    mcp_manager,
    step_dict: dict,
    step_obj,
    step_description: str,
    error: str = "",
    max_candidates: int = 3,
    healing_timeout: float = 10.0,
) -> str | None:
    """Legacy CSS heal. Prefer ``try_heal_ax_rebind`` for UI StructuredStep paths."""

    async def _do_heal():
        original_selector = step_dict.get("description", "") or step_description
        candidates = await heal_selector(
            mcp_manager,
            original_selector=original_selector,
            step_description=step_description,
            error=error,
        )
        if not candidates:
            logger.info("Self-healing: LLM 未返回候选选择器")
            return None

        for candidate in candidates[:max_candidates]:
            selector = candidate["selector"]
            logger.info(
                "Self-healing: 尝试候选选择器 [%.0f%%] %s (理由: %s)",
                float(candidate.get("confidence") or 0) * 100,
                selector,
                (candidate.get("reason") or "")[:60],
            )
            try:
                test_js = f"""
(function() {{
    try {{
        const sel = {_json.dumps(selector)};
        if (sel.startsWith('text=')) {{
            const text = sel.slice(5);
            return document.body.innerText.includes(text) ? 'found' : 'not_found';
        }}
        const el = document.querySelector(sel);
        return el ? 'found' : 'not_found';
    }} catch(e) {{
        return 'error: ' + e.message;
    }}
}})()
"""
                eval_result = await mcp_manager.call_tool(
                    "browser_evaluate",
                    {"function": test_js},
                )
                if eval_result.get("success") and "found" in eval_result.get("text", ""):
                    logger.info("Self-healing: ✅ 选择器有效: %s", selector)
                    return selector
                logger.info("Self-healing: ❌ 选择器无效: %s", selector)
            except (RuntimeError, ConnectionError, OSError, ValueError) as exc:
                logger.info("Self-healing: ❌ 选择器测试异常: %s — %s", selector, exc, exc_info=True)
        return None

    try:
        return await asyncio.wait_for(_do_heal(), timeout=healing_timeout)
    except asyncio.TimeoutError:
        logger.warning("Self-healing timed out after %ss", healing_timeout)
        return None
