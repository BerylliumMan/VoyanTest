# core/chat/contracts.py
# 034 对话编排 JSON 契约：决策解析/校验 + 补丁校验/应用（纯函数，零 IO，便于单测）。
#
# 契约来源：specs/034-chat-case-workbench/contracts/chat-workbench.md C3/C4/C5。
# 原则（032 教训）：动作级容错——单动作校验失败只丢弃该动作，不整条作废。
from __future__ import annotations

import json
import re

ACTION_TYPES = ("generate", "revise_drafts", "edit_case", "run_case", "explain", "set_refs")
REF_TYPES = ("project", "environment", "agent", "case", "batch")
MAX_ACTIONS_PER_TURN = 2
CASE_STEP_FIELDS = ("description", "expected", "target_name", "value", "action", "wait_mode")


# ----------------------------
# 决策解析（C3）
# ----------------------------

def extract_json_object(raw: str) -> dict | None:
    """从模型输出提取第一个平衡的 JSON 对象（容忍代码块与前后缀）。"""
    if not raw:
        return None
    text = raw.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```\s*$", "", text)
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    obj = json.loads(text[start:i + 1])
                except Exception:
                    return None
                return obj if isinstance(obj, dict) else None
    return None


def parse_decision(raw: str) -> tuple[dict | None, str]:
    """解析并归一化一轮决策；返回 (decision, error)。"""
    obj = extract_json_object(raw)
    if obj is None:
        return None, "未能解析出 JSON 决策"
    reply = obj.get("reply")
    if reply is not None and not isinstance(reply, str):
        return None, "reply 必须为字符串"
    actions = obj.get("actions")
    if actions is None:
        actions = []
    if not isinstance(actions, list):
        return None, "actions 必须为数组"
    suggestions = obj.get("suggestions") or []
    if not isinstance(suggestions, list):
        suggestions = []
    return {
        "reply": (reply or "").strip(),
        "thinking": str(obj.get("thinking") or "").strip()[:8000],
        "actions": actions[:MAX_ACTIONS_PER_TURN],
        "suggestions": [str(s).strip()[:100] for s in suggestions if isinstance(s, str) and str(s).strip()][:3],
    }, ""


def _text_fallback(raw: str) -> str:
    """JSON 解析失败时的文本兜底：模型直接输出自然语言（未含动作结构）时保留其回复。

    仅当内容「看起来是自然语言」时启用：非空、不含 JSON 结构片段、长度合理。
    """
    text = (raw or "").strip()
    if not text or len(text) > 4000:
        return ""
    import re as _re

    text = _re.sub(r"^```(?:json|markdown)?\s*", "", text)
    text = _re.sub(r"\s*```\s*$", "", text).strip()
    if not text:
        return ""
    lowered = text.lower()
    if '"actions"' in lowered or '"reply"' in lowered:
        return ""
    if text.startswith("{") or text.startswith("["):
        return ""
    return text


# ----------------------------
# 动作校验（C3）
# ----------------------------

def _validate_refs(items) -> tuple[list, str]:
    out: list = []
    if items is None:
        return out, ""
    if not isinstance(items, list):
        return [], "refs 必须为数组"
    for it in items[:20]:
        if not isinstance(it, dict):
            continue
        t = it.get("type")
        i = it.get("id")
        if t not in REF_TYPES or not isinstance(i, int):
            continue
        out.append({"type": t, "id": i, "label": str(it.get("label") or "")[:200]})
    return out, ""


def validate_action(action) -> tuple[dict | None, str]:
    """白名单 + 逐类型 payload 校验；返回 (归一化动作, 错误)。"""
    if not isinstance(action, dict):
        return None, "action 必须为对象"
    t = action.get("type")
    if t not in ACTION_TYPES:
        return None, f"未知动作类型: {t}"

    if t == "generate":
        kind = action.get("kind") or "ui"
        if kind not in ("functional", "ui"):
            return None, "generate.kind 必须为 functional|ui"
        source = action.get("source") or "text"
        if source not in ("text", "file"):
            return None, "generate.source 必须为 text|file"
        payload = action.get("payload") or {}
        if not isinstance(payload, dict):
            return None, "generate.payload 必须为对象"
        desc = str(payload.get("description") or "").strip()
        if source == "text" and not desc:
            return None, "generate 缺少需求描述"
        return {
            "type": "generate", "kind": kind, "source": source,
            "payload": {"description": desc[:20000], "file_ref": payload.get("file_ref")},
        }, ""

    if t == "revise_drafts":
        session_id = action.get("gen_session_id")
        if not session_id or not isinstance(session_id, str):
            return None, "revise_drafts 缺少 gen_session_id"
        patch, err = validate_draft_patch(action.get("patch"))
        if err:
            return None, err
        return {"type": "revise_drafts", "gen_session_id": session_id, "patch": patch}, ""

    if t == "edit_case":
        case_id = action.get("case_id")
        if not isinstance(case_id, int):
            return None, "edit_case 缺少 case_id"
        patch, err = validate_case_patch(action.get("patch"))
        if err:
            return None, err
        return {
            "type": "edit_case", "case_id": case_id, "patch": patch,
            "summary": str(action.get("summary") or "").strip()[:500],
        }, ""

    if t == "run_case":
        case_ids = action.get("case_ids")
        if not isinstance(case_ids, list) or not case_ids:
            return None, "run_case 缺少 case_ids"
        clean = [c for c in case_ids if isinstance(c, int)]
        if not clean:
            return None, "run_case case_ids 非法"
        mode = action.get("mode")
        if mode is not None and mode not in ("server", "client"):
            return None, "run_case.mode 必须为 server|client"
        agent_id = action.get("agent_id")
        if agent_id is not None and not isinstance(agent_id, int):
            agent_id = None
        if mode == "client" and agent_id is None:
            return None, "run_case mode=client 需要 agent_id"
        env_id = action.get("environment_id")
        if env_id is not None:
            try:
                env_id = int(env_id)
            except (TypeError, ValueError):
                return None, "run_case.environment_id 必须为正整数"
            if env_id <= 0:
                return None, "run_case.environment_id 必须为正整数"
        return {
            "type": "run_case", "case_ids": clean[:20], "mode": mode,
            "agent_id": agent_id, "environment_id": env_id,
        }, ""

    if t == "explain":
        batch_id = action.get("batch_id")
        if not isinstance(batch_id, int):
            return None, "explain 缺少 batch_id"
        case_id = action.get("case_id")
        if case_id is not None and not isinstance(case_id, int):
            case_id = None
        return {"type": "explain", "batch_id": batch_id, "case_id": case_id}, ""

    if t == "set_refs":
        add, err1 = _validate_refs(action.get("add"))
        if err1:
            return None, err1
        remove, err2 = _validate_refs(action.get("remove"))
        if err2:
            return None, err2
        if not add and not remove:
            return None, "set_refs 为空操作"
        return {"type": "set_refs", "add": add, "remove": remove}, ""

    return None, "未支持的动作"  # pragma: no cover


# ----------------------------
# 草稿补丁（C4）
# ----------------------------

def _normalize_case(case: dict, partial: bool = False) -> dict:
    out: dict = {}
    if "title" in case or not partial:
        out["title"] = str(case.get("title") or "").strip()[:500]
    if "steps" in case or not partial:
        out["steps"] = _normalize_steps(case.get("steps"))
    if "expected" in case or not partial:
        out["expected"] = str(case.get("expected") or "").strip()
    if "priority" in case:
        out["priority"] = str(case.get("priority") or "")[:20]
    if "module" in case:
        out["module"] = str(case.get("module") or "")[:255]
    return out


def _normalize_steps(steps) -> list[str]:
    if steps is None:
        return []
    if isinstance(steps, str):
        return [s.strip() for s in steps.splitlines() if s.strip()]
    if isinstance(steps, list):
        out = []
        for s in steps:
            if isinstance(s, str) and s.strip():
                out.append(s.strip())
            elif isinstance(s, dict):
                desc = str(s.get("description") or s.get("step") or "").strip()
                if desc:
                    out.append(desc)
        return out
    return []


def validate_draft_patch(patch) -> tuple[list | None, str]:
    if not isinstance(patch, list) or not patch:
        return None, "patch 必须为非空数组"
    ops: list = []
    for op in patch[:20]:
        if not isinstance(op, dict):
            return None, "patch 项必须为对象"
        kind = op.get("op")
        if kind == "add":
            case = op.get("case")
            if not isinstance(case, dict) or not str(case.get("title") or "").strip():
                return None, "add 需要 case.title"
            item = {"op": "add", "case": _normalize_case(case)}
            ai = op.get("after_index")
            if isinstance(ai, int) and ai >= 0:
                item["after_index"] = ai
            ops.append(item)
        elif kind == "update":
            did = op.get("draft_id")
            if not isinstance(did, int):
                return None, "update 需要 draft_id"
            fields = op.get("fields")
            if not isinstance(fields, dict) or not fields:
                return None, "update 需要 fields"
            ops.append({"op": "update", "draft_id": did, "fields": _normalize_case(fields, partial=True)})
        elif kind == "remove":
            did = op.get("draft_id")
            if not isinstance(did, int):
                return None, "remove 需要 draft_id"
            ops.append({"op": "remove", "draft_id": did})
        else:
            return None, f"未知补丁操作: {kind}"
    return ops, ""


def apply_draft_patch(cases: list[dict], patch: list[dict]) -> tuple[list[dict] | None, list[dict], str]:
    """原子应用草稿补丁；返回 (新列表, 变更记录, 错误)。失败返回 (None, [], 错误)。"""
    result = [dict(c) for c in cases]
    changes: list = []
    by_id = {c.get("draft_id"): idx for idx, c in enumerate(result)}
    for op in patch:
        kind = op["op"]
        if kind == "add":
            ai = op.get("after_index")
            pos = len(result) if ai is None else max(0, min(len(result), ai + 1))
            new_case = dict(op["case"])
            new_case["_new"] = True
            result.insert(pos, new_case)
            changes.append({"kind": "add", "index": pos, "title": new_case.get("title")})
            by_id = {c.get("draft_id"): i for i, c in enumerate(result)}
        elif kind == "update":
            idx = by_id.get(op["draft_id"])
            if idx is None:
                return None, [], f"draft_id={op['draft_id']} 不存在"
            fields = op["fields"]
            changed = [k for k, v in fields.items() if result[idx].get(k) != v]
            result[idx].update(fields)
            changes.append({"kind": "update", "draft_id": op["draft_id"], "fields": changed})
        elif kind == "remove":
            idx = by_id.get(op["draft_id"])
            if idx is None:
                return None, [], f"draft_id={op['draft_id']} 不存在"
            removed = result.pop(idx)
            changes.append({"kind": "remove", "draft_id": op["draft_id"], "title": removed.get("title")})
            by_id = {c.get("draft_id"): i for i, c in enumerate(result)}
    return result, changes, ""


# ----------------------------
# 用例改写补丁（C5）
# ----------------------------

def validate_case_patch(patch) -> tuple[dict | None, str]:
    if not isinstance(patch, dict):
        return None, "patch 必须为对象"
    steps_patch = patch.get("steps")
    case_fields = patch.get("case_fields") or {}
    ops: list = []
    if steps_patch is not None:
        if not isinstance(steps_patch, list):
            return None, "patch.steps 必须为数组"
        for op in steps_patch[:50]:
            if not isinstance(op, dict):
                return None, "步骤补丁项必须为对象"
            kind = op.get("op")
            if kind == "update":
                idx = op.get("index")
                fields = op.get("fields")
                if not isinstance(idx, int) or idx < 0:
                    return None, "update 需要 index"
                if not isinstance(fields, dict) or not fields:
                    return None, "update 需要 fields"
                if "description" in fields and not str(fields["description"]).strip():
                    return None, "description 不能为空"
                clean = {k: str(v)[:2000] for k, v in fields.items() if k in CASE_STEP_FIELDS}
                if not clean:
                    return None, "update fields 无有效字段"
                ops.append({"op": "update", "index": idx, "fields": clean})
            elif kind == "add":
                step = op.get("step")
                if not isinstance(step, dict) or not str(step.get("description") or "").strip():
                    return None, "add 需要 step.description"
                item = {"op": "add", "step": {k: str(step[k])[:2000] for k in CASE_STEP_FIELDS if k in step}}
                ai = op.get("after_index")
                if isinstance(ai, int) and ai >= 0:
                    item["after_index"] = ai
                ops.append(item)
            elif kind == "remove":
                idx = op.get("index")
                if not isinstance(idx, int) or idx < 0:
                    return None, "remove 需要 index"
                ops.append({"op": "remove", "index": idx})
            else:
                return None, f"未知步骤操作: {kind}"
    cf: dict = {}
    if isinstance(case_fields, dict):
        for k in ("name", "priority"):
            if k in case_fields:
                cf[k] = str(case_fields[k])[:200]
    if not ops and not cf:
        return None, "patch 为空"
    return {"steps": ops, "case_fields": cf}, ""


def apply_case_patch(steps: list[dict], patch: dict) -> tuple[list[dict] | None, list[dict], str]:
    """原子应用用例步骤补丁；index/after_index 均相对**原始**步骤列表。

    返回 (新步骤列表, 变更记录, 错误)；失败返回 (None, [], 错误)。
    """
    result = [dict(s) for s in steps]
    n = len(result)
    updates: dict[int, dict] = {}
    removes: set[int] = set()
    adds_after: dict[int, list] = {}
    tail_adds: list = []
    for op in patch.get("steps") or []:
        kind = op["op"]
        if kind == "update":
            idx = op["index"]
            if idx >= n:
                return None, [], f"步骤 index={idx + 1} 超出范围（共 {n} 步）"
            updates[idx] = op["fields"]
        elif kind == "remove":
            idx = op["index"]
            if idx >= n:
                return None, [], f"步骤 index={idx + 1} 超出范围（共 {n} 步）"
            removes.add(idx)
        elif kind == "add":
            ai = op.get("after_index")
            if ai is not None and ai >= n:
                return None, [], f"after_index={ai + 1} 超出范围（共 {n} 步）"
            if ai is None:
                tail_adds.append(op["step"])
            else:
                adds_after.setdefault(ai, []).append(op["step"])
    out: list = []
    changes: list = []
    for idx in range(n):
        if idx in removes:
            changes.append({"kind": "remove", "step_index": idx, "step": result[idx]})
        else:
            if idx in updates:
                for field, after in updates[idx].items():
                    before = result[idx].get(field)
                    if before != after:
                        changes.append({
                            "kind": "update", "step_index": idx, "field": field,
                            "before": before, "after": after,
                        })
                    result[idx][field] = after
            out.append(result[idx])
        for step in adds_after.get(idx, []):
            step2 = dict(step)
            step2.setdefault("description", "")
            pos = len(out)
            out.append(step2)
            changes.append({"kind": "add", "step_index": pos, "step": step2})
    for step in tail_adds:
        step2 = dict(step)
        step2.setdefault("description", "")
        pos = len(out)
        out.append(step2)
        changes.append({"kind": "add", "step_index": pos, "step": step2})
    if not out:
        return None, [], "补丁应用后步骤为空"
    return out, changes, ""
