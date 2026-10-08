"""生成后处理：确定性前置补齐。

用例首步如果不是"打开/登录/导航"，浏览器从零开始无法执行（校验器也会拦）。
这里用确定性规则补上：
  · 一律补「打开首页」
  · 若用例明显依赖登录态（步骤/标题出现商品列表、购物车、结算、工作台等），
    且素材里能拿到测试账号，则补完整登录序列（打开首页 → 用户名 → 密码 → 点击登录）
幂等：首步已是打开/登录/导航的用例不动；重复运行不叠加。
"""
from __future__ import annotations

import re
from typing import Any

_ENTRY_RE = re.compile(r"^\s*(?:\d+[\.、]\s*)?(打开|进入|导航|访问|前往|登录|打开首页)")
_LOGIN_WORD_RE = re.compile(r"登录")
_POST_LOGIN_RE = re.compile(
    r"(商品列表|购物车|结算|下单|订单|列表页|详情页|工作台|控制台|个人中心|用户中心|退出登录|仪表盘|看板)"
)

_CRED_PATTERNS = [
    re.compile(r"账号[：:]\s*([\w.@+-]+)\s*[/／、,，]\s*([\w.!@#$%^&*+-]+)"),
    re.compile(r"账号[：:]\s*([\w.@+-]+)\s+密码[：:]\s*([\w.!@#$%^&*+-]+)"),
    re.compile(r"用户名[：:]\s*([\w.@+-]+)[\s，,、]+密码[：:]\s*([\w.!@#$%^&*+-]+)"),
    re.compile(r"[Uu]sername[：:]\s*(\S+?)\s*[/／,，]\s*[Pp]assword[：:]\s*(\S+)"),
    re.compile(r"测试账号[：:]\s*(\S+?)\s*[/／]\s*(\S+)"),
]

ENTRY_TEXT = "打开首页"


def find_login_credentials(text: str | None) -> tuple[str, str] | None:
    """从素材/前置条件里找测试账号（账号 + 密码）。"""
    blob = str(text or "")
    for pat in _CRED_PATTERNS:
        m = pat.search(blob)
        if m:
            user, pwd = m.group(1).strip(), m.group(2).strip()
            if user and pwd and len(user) <= 64 and len(pwd) <= 64:
                return user, pwd
    return None


def _case_text(case: dict[str, Any]) -> str:
    parts = [
        str(case.get("title") or ""),
        str(case.get("precondition") or ""),
        str(case.get("expected_result") or ""),
    ]
    steps = case.get("steps") or []
    for s in steps:
        parts.append(str(s.get("description") if isinstance(s, dict) else s))
    return "\n".join(parts)


def _is_entry_step(text: str) -> bool:
    return bool(_ENTRY_RE.match(str(text or "").strip()))


def _login_structured(user: str, pwd: str) -> list[dict[str, Any]]:
    return [
        {"action": "goto", "target_name": "首页"},
        {"action": "fill", "target_name": "Username", "target_role": "textbox", "value": user},
        {"action": "fill", "target_name": "Password", "target_role": "textbox", "value": pwd},
        {"action": "click", "target_name": "Login", "target_role": "button"},
    ]


def _goto_structured() -> dict[str, Any]:
    return {"action": "goto", "target_name": "首页"}


def ensure_entry_steps(
    cases: list[dict[str, Any]],
    *,
    source_text: str = "",
    default_username: str = "",
    default_password: str = "",
) -> dict[str, int]:
    """给"首步不可独立执行"的用例补齐前置步骤。就地修改并返回统计。"""
    stats = {"fixed": 0, "already": 0, "with_login": 0}
    creds: tuple[str, str] | None = None
    if default_username and default_password:
        creds = (default_username, default_password)
    else:
        creds = find_login_credentials(source_text) or find_login_credentials(
            "\n".join(_case_text(c) for c in cases)
        )

    from app.gen.response_parser import _to_numbered_expected, _to_numbered_text
    from core.step_normalize import render_structured_step

    for case in cases or []:
        if not isinstance(case, dict):
            continue
        steps = case.get("steps") or []
        first_text = ""
        for s in steps:
            first_text = str(s.get("description") if isinstance(s, dict) else s)
            break
        if not first_text:
            first_text = (case.get("test_steps") or "").splitlines()[0] if case.get("test_steps") else ""
        if _is_entry_step(first_text):
            stats["already"] += 1
            continue

        title = str(case.get("title") or "")
        body = _case_text(case)
        needs_login = bool(
            creds
            and not _LOGIN_WORD_RE.search(title)
            and not _LOGIN_WORD_RE.search(body)
            and _POST_LOGIN_RE.search(body)
        )

        prefix_struct: list[dict[str, Any]] = []
        if needs_login and creds:
            prefix_struct = _login_structured(creds[0], creds[1])
            prefix_text = [
                ENTRY_TEXT,
                f"在【Username】输入框输入「{creds[0]}」",
                f"在【Password】密码框输入「{creds[1]}」",
                "点击【Login】登录按钮",
            ]
            stats["with_login"] += 1
        else:
            prefix_struct = [_goto_structured()]
            prefix_text = [ENTRY_TEXT]

        # 1) 结构化步骤
        structured = [s for s in (case.get("structured_steps") or []) if isinstance(s, dict)]
        case["structured_steps"] = prefix_struct + structured

        # 2) steps（含 description/expected）
        new_steps: list[dict[str, Any]] = []
        for i, s in enumerate(prefix_struct):
            desc = prefix_text[i] if i < len(prefix_text) else render_structured_step(s)
            new_steps.append({**s, "description": desc, "expected": ""})
        for s in steps:
            new_steps.append(s if isinstance(s, dict) else {"description": str(s), "expected": ""})
        case["steps"] = new_steps

        # 3) 文本与预期编号列
        descs = [s.get("description") or render_structured_step(s) for s in new_steps]
        case["test_steps"] = _to_numbered_text(descs)
        expected = []
        for s in new_steps:
            expected.append(s.get("expected") or "")
        case["expected_result"] = _to_numbered_expected(expected)

        stats["fixed"] += 1
    return stats


_ASSERT_ACTIONS = {"assert_text", "assert_visible", "assert_contains", "assert"}
_VAGUE_TEXT_RE = __import__("re").compile(r"^(可见|正常|正确|成功|完成|可见性)$")


def backfill_assert_values(cases: list[dict[str, Any]]) -> int:
    """结构化断言缺 value、但描述里有【X】/「X」→ 回填 value=X（信息已在文本里，语义无损）。

    幂等；只动断言类步骤；描述里没有括号内容的不动（那是真问题，交给校验器拦）。
    """
    from core.script_precompile import extract_bracketed_expected

    fixed = 0
    for case in cases or []:
        if not isinstance(case, dict):
            continue
        desc_by_index: dict[int, str] = {}
        for i, st in enumerate(case.get("steps") or []):
            if isinstance(st, dict):
                desc_by_index[i] = str(st.get("description") or "")
        for i, st in enumerate(case.get("structured_steps") or []):
            if not isinstance(st, dict):
                continue
            if str(st.get("action") or "") not in _ASSERT_ACTIONS:
                continue
            value = st.get("value")
            if value is not None and str(value).strip():
                continue
            desc = str(st.get("description") or "") or desc_by_index.get(i, "")
            text = extract_bracketed_expected(desc)
            if not text and str(st.get("action") or "") in ("assert_text", "assert_contains"):
                # 模型常把期望文案写在 target_name 上（渲染器也按此渲染）→ 迁移到 value
                cand = str(st.get("target_name") or "").strip()
                if cand and not _VAGUE_TEXT_RE.search(cand):
                    text = cand
            if not text:
                continue
            st["value"] = text
            fixed += 1
        # steps 镜像同步
        for i, st in enumerate(case.get("steps") or []):
            if isinstance(st, dict) and str(st.get("action") or "") in _ASSERT_ACTIONS:
                if st.get("value") is None or str(st.get("value")).strip() == "":
                    pair = (case.get("structured_steps") or [{}] * (i + 1))[i] if i < len(case.get("structured_steps") or []) else {}
                    if isinstance(pair, dict) and pair.get("value"):
                        st["value"] = pair["value"]
    return fixed
