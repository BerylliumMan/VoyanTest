# app/gen/api/llm_cases.py — 接口用例的 LLM 增强（029-api-testing T025）
#
# 定位：规则生成器（app/gen/api/generator.py）负责确定性覆盖；本模块在其之上
# 补充边界/类型错误/鉴权失败等场景。**硬规则不通过即丢弃草案**（不静默修正），
# 与 UI 用例生成的校验哲学一致（见 app/gen/agents/validator.py）。
from __future__ import annotations

import json
import logging
import re
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.gen.api.prompts import API_CASE_GENERATE_PROMPT
from app.models.schemas import ApiSpecPayload
from core.api_spec import ApiSpecError, normalize_api_spec, validate_api_spec

logger = logging.getLogger(__name__)

MIN_PROMPT_CHARS = 80
_NARRATIVE_MARKERS = (
    "该用户",
    "该订单",
    "某个",
    "某一个",
    "有效值",
    "示例值",
    "合适的",
    "任意",
    "待定",
    "TBD",
)
_ALLOWED_PRIORITIES = {"high", "medium", "low"}


def case_violations(case: Any) -> list[str]:
    """返回草案违反的硬规则列表（空列表 = 通过）。"""
    problems: list[str] = []
    if not isinstance(case, dict):
        return ["草案不是对象"]
    if not str(case.get("name") or "").strip():
        problems.append("用例名不能为空")
    priority = str(case.get("priority") or "medium")
    if priority not in _ALLOWED_PRIORITIES:
        problems.append(f"priority 非法: {priority!r}")
    spec = case.get("api_spec")
    try:
        normalized = normalize_api_spec(spec)
    except ApiSpecError as exc:
        return problems + [f"api_spec 结构非法: {exc}"]
    spec_problems = validate_api_spec(normalized)
    if spec_problems:
        problems.extend(spec_problems[:5])
    for step in normalized.get("steps", []):
        request = step.get("request") or {}
        if not str(request.get("url") or "").strip():
            problems.append("请求 URL 不能为空")
        body = request.get("body") or {}
        if str(body.get("type")) == "json" and body.get("content"):
            problems.extend(_body_value_violations(str(body["content"])))
        assertions = step.get("assertions") or []
        if not assertions:
            problems.append("每个用例至少 1 条断言（断言必须可判定）")
        for idx, assertion in enumerate(assertions):
            if not isinstance(assertion, dict):
                problems.append(f"assertions[{idx}] 不是对象")
                continue
            condition = str(assertion.get("condition") or "equals")
            if not str(assertion.get("type") or "").strip():
                problems.append(f"assertions[{idx}] 缺少 type")
            if condition not in ("exists", "not_exists") and str(
                assertion.get("expected") or ""
            ).strip() == "":
                problems.append(f"assertions[{idx}] 缺少 expected（断言不可判定）")
    return problems


def _body_value_violations(content: str) -> list[str]:
    try:
        payload = json.loads(content)
    except Exception:
        return ["请求体声明为 json 但不是合法 JSON"]
    problems: list[str] = []

    def _walk(node: Any, path: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                _walk(value, f"{path}.{key}" if path else str(key))
            return
        if isinstance(node, list):
            for index, value in enumerate(node):
                _walk(value, f"{path}[{index}]")
            return
        if isinstance(node, str):
            text = node.strip()
            if text == "":
                problems.append(f"参数 {path or 'body'} 的值为空（必须给具体值）")
                return
            if re.search(r"\{\{.*\}\}", text):
                return  # 变量占位由渲染期解析，属于明确值来源
            for marker in _NARRATIVE_MARKERS:
                if marker in text:
                    problems.append(f"参数 {path or 'body'} 的值不是具体值: {text!r}")
                    return

    _walk(payload, "")
    return problems


def parse_llm_cases(text: str) -> list[dict]:
    """从 LLM 文本中解析用例数组（容忍代码块与 {cases:[...]} 包装）。"""
    from app.gen.response_parser import _extract_json

    blob = _extract_json(text) or text
    try:
        data = json.loads(blob)
    except Exception:
        return []
    if isinstance(data, dict):
        for key in ("cases", "items", "data", "test_cases"):
            if isinstance(data.get(key), list):
                return [c for c in data[key] if isinstance(c, dict)]
        return []
    if isinstance(data, list):
        return [c for c in data if isinstance(c, dict)]
    return []


async def enhance_cases_with_llm(
    db: AsyncSession,
    definition: dict,
    base_cases: list[dict],
    options: dict | None = None,
    *,
    agent_id: int | None = None,
) -> tuple[list[dict], list[str]]:
    """用 LLM 追加边界/异常用例；返回 (通过硬规则的草案, 备注列表)。"""
    from app.gen.model_client import call_model
    from app.runtime_config import resolve_prompt_for_agent

    options = options or {}
    notes: list[str] = []
    try:
        prompt = await resolve_prompt_for_agent(
            db,
            agent_type="generation",
            prompt_key="api_case_generate",
            variables={},
            agent_id=agent_id,
        )
    except Exception as exc:
        logger.warning("解析 api_case_generate 提示词失败，回退内置常量: %s", exc)
        prompt = ""
    if not prompt or len(prompt.strip()) < MIN_PROMPT_CHARS:
        if prompt:
            logger.warning(
                "gen prompt too short for api_case_generate (%d chars)，回退内置常量",
                len(prompt.strip()),
            )
        prompt = API_CASE_GENERATE_PROMPT

    user_payload = json.dumps(
        {
            "definition": {
                "name": definition.get("name"),
                "method": definition.get("method"),
                "path": definition.get("path"),
                "request_schema": definition.get("request_schema") or {},
                "response_schema": definition.get("response_schema") or {},
            },
            "existing_cases": [
                {"name": c.get("name"), "notes": c.get("notes") or []} for c in base_cases
            ],
            "options": {
                k: bool(options.get(k, False))
                for k in ("boundary", "type_error", "auth_fail")
            },
        },
        ensure_ascii=False,
    )
    try:
        text = await call_model(
            [
                {"role": "system", "content": prompt},
                {"role": "user", "content": user_payload},
            ],
            agent_type="generation",
            agent_id=agent_id,
        )
    except Exception as exc:
        logger.warning("接口用例 LLM 增强失败: %s", exc, exc_info=True)
        return [], [f"LLM 增强失败: {exc}"]

    raw_cases = parse_llm_cases(str(text or ""))
    if not raw_cases:
        return [], ["LLM 未返回可用用例（输出不是合法 JSON 数组）"]

    accepted: list[dict] = []
    for raw in raw_cases:
        problems = case_violations(raw)
        if problems:
            notes.append(
                f"丢弃草案 {raw.get('name') or '<未命名>'}: {'; '.join(problems[:3])}"
            )
            continue
        try:
            spec = normalize_api_spec(raw["api_spec"])
        except ApiSpecError as exc:
            notes.append(f"丢弃草案 {raw.get('name') or '<未命名>'}: {exc}")
            continue
        try:
            ApiSpecPayload.model_validate(spec)
        except Exception as exc:
            notes.append(f"丢弃草案 {raw.get('name') or '<未命名>'}: 载荷校验失败 {exc}")
            continue
        accepted.append(
            {
                "name": str(raw.get("name") or "").strip(),
                "priority": str(raw.get("priority") or "medium"),
                "api_spec": spec,
                "notes": [str(n) for n in (raw.get("notes") or [])],
                "source": "llm",
            }
        )
    return accepted, notes


__all__ = ["MIN_PROMPT_CHARS", "case_violations", "parse_llm_cases", "enhance_cases_with_llm"]
