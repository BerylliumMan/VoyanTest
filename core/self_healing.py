"""AI-powered selector healing for failed element location.

当测试步骤的元素定位失败时，调用 LLM 分析当前页面的 DOM 快照，
生成备选选择器列表，按置信度降序排列。
"""

import asyncio
import json as _json
import logging
import os

import openai

logger = logging.getLogger(__name__)

# 模块级 LLM 客户端缓存：整个进程生命周期内只创建一次
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
        # ValueError: AI 配置缺失；RuntimeError: 配置加载异常
        logger.warning("Failed to create LLM client: %s", exc, exc_info=True)
        return None
    return _cached_client


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


async def heal_selector(
    mcp_manager,
    original_selector: str,
    step_description: str,
    error: str = "",
) -> list[dict]:
    """调用 LLM 分析 DOM，返回候选选择器列表。

    Args:
        mcp_manager: MCP 管理器，具有 async call_tool(name, args) 方法
        original_selector: 失败步骤的原始选择器
        step_description: 步骤的自然语言描述
        error: 原始错误消息

    Returns:
        [{"selector": "text=登录", "confidence": 0.95, "reason": "..."}, ...]
    """
    # 1. 获取 DOM 快照
    try:
        snapshot_result = await mcp_manager.call_tool("browser_snapshot", {})
        dom_snapshot = snapshot_result.get("text", "") if snapshot_result.get("success") else ""
    except (RuntimeError, ConnectionError, OSError) as exc:
        logger.warning("Failed to get DOM snapshot for healing: %s", exc, exc_info=True)
        return []

    if not dom_snapshot or len(dom_snapshot) < 10:
        return []

    # 2. 构建 prompt
    prompt = _HEALING_PROMPT.format(
        step_description=step_description,
        original_selector=original_selector,
        error=error,
        dom_snapshot=dom_snapshot[:8000],  # 截断，避免 token 超限
    )

    # 3. 调用 LLM
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
        # 尝试解析 JSON（LLM 可能在 JSON 前后加 markdown 标记）
        content = content.strip()
        if content.startswith("```"):
            content = content.split("```")[1]
            if content.startswith("json"):
                content = content[4:]
        candidates = _json.loads(content)
    except Exception as exc:  # noqa: BLE001 - 自愈 LLM 调用：吞掉所有异常返回空候选
        logger.warning("LLM healing failed: %s", exc, exc_info=True)
        return []

    # 4. 验证返回格式
    if not isinstance(candidates, list):
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
    """尝试修复选择器，逐个测试候选，返回第一个成功的选择器。

    Args:
        mcp_manager: MCP 管理器
        step_dict: 步骤字典，包含 description 字段
        step_obj: 步骤对象（保留接口兼容，当前未使用）
        step_description: 步骤的自然语言描述
        error: 原始错误消息
        max_candidates: 最多测试的候选数
        healing_timeout: 整体自愈超时时间（秒），默认 10 秒

    Returns:
        成功的选择器字符串，或 None（全部失败或超时）
    """

    async def _do_heal():
        original_selector = step_dict.get("description", "") or step_description

        # 获取候选
        candidates = await heal_selector(
            mcp_manager,
            original_selector=original_selector,
            step_description=step_description,
            error=error,
        )

        if not candidates:
            logger.info("Self-healing: LLM 未返回候选选择器")
            return None

        # 逐个尝试
        for candidate in candidates[:max_candidates]:
            selector = candidate["selector"]
            logger.info(
                f"Self-healing: 尝试候选选择器 [{candidate['confidence']:.0%}] {selector} "
                f"(理由: {candidate['reason'][:60]})"
            )

            try:
                # 简单测试：用 browser_evaluate 检查元素是否存在
                test_js = f"""
(function() {{
    try {{
        // 对于 text= 选择器，搜索 DOM 文本
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
                    {"expression": test_js},
                )

                if eval_result.get("success") and "found" in eval_result.get("text", ""):
                    logger.info("Self-healing: ✅ 选择器有效: %s", selector)
                    return selector
                else:
                    logger.info("Self-healing: ❌ 选择器无效: %s", selector)
            except (RuntimeError, ConnectionError, OSError, ValueError) as exc:
                logger.info("Self-healing: ❌ 选择器测试异常: %s — %s", selector, exc, exc_info=True)

        return None

    try:
        return await asyncio.wait_for(_do_heal(), timeout=healing_timeout)
    except asyncio.TimeoutError:
        logger.warning("Self-healing timed out after %ss", healing_timeout)
        return None
