"""033（US1）：接口用例静态体检（Inspections）。

纯静态、**零出站**；结论为"提示层"——不阻断保存与执行（保存走既有
``core.api_spec.validate_api_spec`` 硬校验，行为不变）。

规则码（契约 C1 / data-model §6）：
    unresolved_variable / url_empty / url_relative_without_base /
    header_empty_value / duplicate_keys / body_json_invalid /
    content_type_mismatch / jsonpath_syntax / secret_in_url /
    dataset_missing_or_mismatch
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Iterator, Optional

# 单一来源：变量正则/内置变量/secret 判定全部复用执行层实现（避免双份漂移）
from core.api_runner.variables import _VAR_RE
from core.api_runner.variables import _builtin as _builtin_value
from core.api_runner.variables import secret_keys

__all__ = ["Finding", "inspect_api_spec"]

_SCRIPT_ASSIGN_RE = re.compile(r"(?m)^[ \t]*([A-Za-z_][A-Za-z0-9_]*)[ \t]*=[^=]")
_SCRIPT_FOR_RE = re.compile(r"(?m)^[ \t]*for[ \t]+([A-Za-z_][A-Za-z0-9_]*)[ \t]+in[ \t]")


@dataclass
class Finding:
    """单条体检验证结论（不落库）。"""

    level: str  # "error" | "warning"
    code: str
    message: str
    step_index: int = -1  # 0 基；非步骤级（spec 级）= -1
    field: str = ""  # 如 "request.url" / "variables[0].value"
    hint: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "level": self.level,
            "code": self.code,
            "message": self.message,
            "step_index": self.step_index,
            "field": self.field,
            "hint": self.hint,
        }


# ── 遍历工具 ────────────────────────────────────────────────────────────────


def _walk_strings(value: Any, path: str) -> Iterator[tuple[str, str]]:
    """递归收集 (路径, 字符串值)。"""
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, dict):
        for k, v in value.items():
            yield from _walk_strings(v, f"{path}.{k}" if path else str(k))
    elif isinstance(value, list):
        for i, v in enumerate(value):
            yield from _walk_strings(v, f"{path}[{i}]")


def _enabled(item: Any) -> bool:
    return bool(item.get("enable", True)) if isinstance(item, dict) else False


def _enabled_items(items: Any) -> list[dict]:
    return [it for it in (items or []) if _enabled(it)]


def _template_names(text: str) -> list[str]:
    return [m.group(1).strip() for m in _VAR_RE.finditer(text or "") if m.group(1).strip()]


def _content_type_header(headers: Any) -> Optional[str]:
    for it in _enabled_items(headers):
        if str(it.get("key") or "").strip().lower() == "content-type":
            value = str(it.get("value") or "").strip()
            if value:
                return value
    return None


# ── 主入口 ──────────────────────────────────────────────────────────────────


def inspect_api_spec(
    spec: Any,
    *,
    environment: Optional[dict] = None,
    dataset: Optional[dict] = None,
) -> list[Finding]:
    """对一份 api_spec 执行全规则静态体检。

    ``environment``：``{"base_url": str, "variables": [...], "services": {...}}``；
    ``dataset``：``{"columns": [...], "rows": [...]}``（不带行数据时仅校验列）。
    """
    spec = spec if isinstance(spec, dict) else {}
    env = environment or {}
    steps = spec.get("steps") if isinstance(spec.get("steps"), list) else []

    findings: list[Finding] = []

    # ── 可用变量集合（静态可解析的部分）────────────────────────────────────
    available: set[str] = set()
    for it in spec.get("variables") or []:
        if isinstance(it, dict) and it.get("key"):
            available.add(str(it["key"]))
    for st in steps:
        if not isinstance(st, dict):
            continue
        for it in st.get("variables") or []:
            if isinstance(it, dict) and it.get("key"):
                available.add(str(it["key"]))
    if dataset:
        for col in dataset.get("columns") or []:
            if col:
                available.add(str(col))
    for name in (env.get("services") or {}):
        if name:
            available.add(str(name))
    for it in env.get("variables") or []:
        if isinstance(it, dict) and it.get("key"):
            available.add(str(it["key"]))
    if env.get("base_url"):
        available.add("baseUrl")
    # 提取器产出（所有步骤；顺序无关的保守口径）
    for st in steps:
        if not isinstance(st, dict):
            continue
        for ex in st.get("extractors") or []:
            if isinstance(ex, dict) and str(ex.get("variable") or "").strip():
                available.add(str(ex["variable"]).strip())
    # 脚本赋值产出（pre/post script 的简单赋值与 for 目标）
    for st in steps:
        if not isinstance(st, dict):
            continue
        for script_key in ("pre_script", "post_script"):
            text = str(st.get(script_key) or "")
            if not text:
                continue
            available.update(_SCRIPT_ASSIGN_RE.findall(text))
            available.update(_SCRIPT_FOR_RE.findall(text))

    secret_names: set[str] = set()
    secret_names |= secret_keys(spec.get("variables"))
    secret_names |= secret_keys(env.get("variables"))

    def _check_unresolved(text: str, step_index: int, field: str) -> None:
        for name in _template_names(text):
            if name in available or _builtin_value(name) is not None:
                continue
            hint = None
            if name.startswith("$"):
                hint = "内置变量名拼写错误或不存在"
            findings.append(
                Finding(
                    level="error",
                    code="unresolved_variable",
                    message=(
                        f"变量 {{{{{name}}}}} 未解析（"
                        f"{f'步骤 {step_index + 1} 的 ' if step_index >= 0 else '用例级 '}{field}）"
                    ),
                    step_index=step_index,
                    field=field,
                    hint=hint,
                )
            )

    # ── spec 级（variables 值与潜在 auth 等；跳过 steps 子树）──────────────
    spec_level = {k: v for k, v in spec.items() if k != "steps"}
    for path, text in _walk_strings(spec_level, ""):
        if "{{" in text:
            _check_unresolved(text, -1, path)

    # ── 数据集绑定 ─────────────────────────────────────────────────────────
    dataset_id = spec.get("dataset_id")
    if dataset_id:
        if not dataset:
            findings.append(
                Finding(
                    level="error",
                    code="dataset_missing_or_mismatch",
                    message=f"用例绑定了数据集 #{dataset_id}，但该数据集不存在或未提供",
                    step_index=-1,
                    field="dataset_id",
                    hint="重新绑定数据集或移除绑定",
                )
            )
        else:
            if not [c for c in (dataset.get("columns") or []) if str(c).strip()]:
                findings.append(
                    Finding(
                        level="error",
                        code="dataset_missing_or_mismatch",
                        message=f"数据集 #{dataset_id} 没有列定义（无法生成变量）",
                        step_index=-1,
                        field="dataset_id",
                    )
                )
            elif not (dataset.get("rows") or []):
                findings.append(
                    Finding(
                        level="warning",
                        code="dataset_missing_or_mismatch",
                        message=f"数据集 #{dataset_id} 没有数据行（绑定后不会产生迭代）",
                        step_index=-1,
                        field="dataset_id",
                    )
                )

    # ── 逐步骤规则 ─────────────────────────────────────────────────────────
    seen: set[tuple] = set()

    def _add(item: Finding) -> None:
        key = (item.level, item.code, item.step_index, item.field, item.message)
        if key not in seen:
            seen.add(key)
            findings.append(item)

    for idx, st in enumerate(steps):
        if not isinstance(st, dict):
            continue
        request = st.get("request") if isinstance(st.get("request"), dict) else {}
        enabled = _enabled(st) if "enable" in st else True
        if not enabled:
            continue
        url = str(request.get("url") or "")
        body = request.get("body") if isinstance(request.get("body"), dict) else {}
        headers = request.get("headers")
        query = request.get("query")

        # unresolved：步骤内全部字符串
        for path, text in _walk_strings(st, ""):
            if "{{" not in text:
                continue
            field = path
            if field.startswith(f"steps[{idx}]"):
                field = field[len(f"steps[{idx}]") :].lstrip(".")
            if not field:
                field = path
            _check_unresolved(text, idx, field)

        # url_empty / url_relative_without_base
        if not url.strip():
            _add(
                Finding(
                    level="error",
                    code="url_empty",
                    message=f"步骤 {idx + 1} 的请求 URL 为空",
                    step_index=idx,
                    field="request.url",
                )
            )
        elif "{{" not in url:
            lower = url.strip().lower()
            if not (lower.startswith("http://") or lower.startswith("https://")):
                if not str(env.get("base_url") or "").strip():
                    _add(
                        Finding(
                            level="warning",
                            code="url_relative_without_base",
                            message=f"步骤 {idx + 1} 的 URL 非绝对地址（{url.strip()[:60]}），且环境未配置 baseUrl",
                            step_index=idx,
                            field="request.url",
                            hint="补全完整地址，或在环境中配置 baseUrl",
                        )
                    )

        # header_empty_value
        for i, it in enumerate(_enabled_items(headers)):
            key = str(it.get("key") or "").strip()
            if key and str(it.get("value") or "") == "":
                _add(
                    Finding(
                        level="warning",
                        code="header_empty_value",
                        message=f"请求头 {key} 为空值",
                        step_index=idx,
                        field=f"request.headers[{i}].value",
                    )
                )

        # duplicate_keys（headers/query/form_data，大小写不敏感）
        for where, items in (
            ("headers", headers),
            ("query", query),
            ("form_data", body.get("form_data")),
        ):
            groups: dict[str, list[str]] = {}
            for it in _enabled_items(items):
                key = str(it.get("key") or "").strip()
                if key:
                    groups.setdefault(key.lower(), []).append(key)
            for keys in groups.values():
                if len(keys) > 1:
                    _add(
                        Finding(
                            level="warning",
                            code="duplicate_keys",
                            message=f"request.{where} 存在重复键 {keys[0]!r}（{len(keys)} 次）",
                            step_index=idx,
                            field=f"request.{where}",
                        )
                    )

        # body_json_invalid / content_type_mismatch
        body_type = str(body.get("type") or "none")
        content = str(body.get("content") or "")
        if body_type == "json" and content.strip():
            candidate = _VAR_RE.sub("0", content)
            try:
                json.loads(candidate)
            except (TypeError, ValueError) as exc:
                _add(
                    Finding(
                        level="error",
                        code="body_json_invalid",
                        message=f"步骤 {idx + 1} 的 JSON 请求体不是合法 JSON：{str(exc)[:120]}",
                        step_index=idx,
                        field="request.body.content",
                    )
                )
            ct = _content_type_header(headers)
            if ct is not None and "json" not in ct.lower():
                _add(
                    Finding(
                        level="warning",
                        code="content_type_mismatch",
                        message=f"请求体为 JSON，但 Content-Type 为 {ct!r}",
                        step_index=idx,
                        field="request.headers",
                        hint="建议改为 application/json",
                    )
                )

        # jsonpath_syntax
        for ex in st.get("extractors") or []:
            if not isinstance(ex, dict) or str(ex.get("type") or "") != "jsonpath":
                continue
            expression = str(ex.get("expression") or "").strip()
            if not expression:
                continue
            candidate = _VAR_RE.sub("0", expression)
            try:
                from jsonpath_ng.ext import parse as _jp_parse

                _jp_parse(candidate)
            except Exception as exc:  # noqa: BLE001 - 任意解析异常都视为语法错误
                _add(
                    Finding(
                        level="error",
                        code="jsonpath_syntax",
                        message=f"步骤 {idx + 1} 的 jsonpath 语法错误：{expression[:80]}（{str(exc)[:80]}）",
                        step_index=idx,
                        field="extractors.expression",
                    )
                )

        # secret_in_url（URL 与查询参数中的 secret 变量）
        url_and_query = [("request.url", url)]
        for i, it in enumerate(_enabled_items(query)):
            url_and_query.append((f"request.query[{i}].value", str(it.get("value") or "")))
            url_and_query.append((f"request.query[{i}].key", str(it.get("key") or "")))
        for field, text in url_and_query:
            for name in _template_names(text):
                if name in secret_names:
                    _add(
                        Finding(
                            level="warning",
                            code="secret_in_url",
                            message=f"敏感变量 {{{{{name}}}}} 出现在 URL/查询参数（可能被日志/代理记录）",
                            step_index=idx,
                            field=field,
                            hint="改用请求头/请求体传递敏感值",
                        )
                    )

    findings.sort(key=lambda f: (f.step_index, f.code, f.field, f.message))
    return findings
