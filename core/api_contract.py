# core/api_contract.py
# 040 US5：接口契约确定性 diff（纯函数，零网络/零模型）
from __future__ import annotations

from typing import Any

_TYPE_MAP = {
    "string": str, "integer": int, "number": (int, float),
    "boolean": bool, "array": list, "object": dict,
}


def _type_ok(value: Any, expected: str) -> bool:
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    py = _TYPE_MAP.get(expected)
    return py is None or isinstance(value, py)


def _diff_object(schema: dict, value: Any, path: str, out: list[dict]) -> None:
    if not isinstance(value, dict):
        out.append({"kind": "type_changed", "path": path, "expected": "object",
                    "actual": type(value).__name__, "severity": "breaking"})
        return
    props = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    required = schema.get("required") if isinstance(schema.get("required"), list) else []
    for name in required:
        if name not in value:
            out.append({"kind": "missing_required", "path": f"{path}.{name}",
                        "expected": "present", "actual": "missing", "severity": "breaking"})
    for name, subschema in props.items():
        if name not in value or not isinstance(subschema, dict):
            continue
        child = f"{path}.{name}" if path else name
        expected_type = subschema.get("type")
        if expected_type and not _type_ok(value[name], str(expected_type)):
            out.append({"kind": "type_changed", "path": child, "expected": expected_type,
                        "actual": type(value[name]).__name__, "severity": "breaking"})
            continue
        if expected_type == "object" and isinstance(value[name], dict):
            _diff_object(subschema, value[name], child, out)
        if expected_type == "array" and isinstance(value[name], list) and isinstance(subschema.get("items"), dict):
            for idx, item in enumerate(value[name][:5]):
                if isinstance(item, dict) and subschema["items"].get("type") == "object":
                    _diff_object(subschema["items"], item, f"{child}[{idx}]", out)
    for name in value:
        if props and name not in props:
            out.append({"kind": "added_field", "path": f"{path}.{name}" if path else name,
                        "expected": "not_defined", "actual": type(value[name]).__name__,
                        "severity": "compatible"})


def diff_response(
    response_schema: dict | None,
    status: int | None,
    body: Any,
) -> dict:
    findings: list[dict] = []
    schema = response_schema if isinstance(response_schema, dict) else {}
    statuses = schema.get("statuses") if isinstance(schema.get("statuses"), dict) else {}
    if not statuses:
        return {"status": "missing_evidence", "breaking": False, "findings": [],
                "summary": "接口定义没有响应 schema，无法做契约检测"}
    key = str(status) if status is not None else ""
    entry = statuses.get(key) if key in statuses else statuses.get(f"{key}xx")
    if entry is None:
        findings.append({"kind": "status_changed", "path": "response.status",
                         "expected": sorted(statuses.keys()), "actual": status,
                         "severity": "breaking"})
        return {"status": "checked", "breaking": True, "findings": findings,
                "summary": f"响应状态码 {status} 未在接口定义中声明"}
    subschema = entry.get("schema") if isinstance(entry, dict) else None
    if not isinstance(subschema, dict) or not subschema:
        return {"status": "missing_evidence", "breaking": False, "findings": [],
                "summary": "该状态码没有响应 schema，无法做契约检测"}
    _diff_object(subschema, body, "", findings)
    breaking = any(f["severity"] == "breaking" for f in findings)
    return {
        "status": "checked",
        "breaking": breaking,
        "findings": findings,
        "summary": (
            f"发现 {len(findings)} 处契约差异（{'破坏性' if breaking else '兼容'}）"
            if findings else "未发现契约差异"
        ),
    }
