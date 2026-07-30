"""Rewrite NL step text into Instant-style phrases before Intent / LLM execution.

Conservative, deterministic transforms only — no LLM. Aligns with Midscene Instant
Action phrasing used by generation (点击【x】 / 在【y】输入 z).
"""
from __future__ import annotations

import re

_CTRL_TYPE_SUFFIX = (
    r"(?:下拉框|下拉菜单|下拉列表|选择器|输入框|文本框|文本域|编辑框|密码框|"
    r"组合框|按钮|控件|弹窗|对话框|提示框)"
)

# Field label candidates that commonly appear without「输入框」suffix
_FIELD_HINT = r"(?:用户名|密码|账号|账户|手机号|手机|邮箱|验证码|搜索|关键字|关键词|姓名|名称|单位|部门)"


def optimize_step_for_execution(desc: str) -> str:
    """Normalize a single step description for execution Intent binding.

    Examples:
      密码输入【Abc12345】     → 在【密码】输入 Abc12345
      在【密码】输入【x】      → 在【密码】输入 x
      点击登录按钮             → 点击【登录】
      单位下拉框选择【汉东】   → 在【单位】中选择【汉东】
    """
    s = (desc or "").strip()
    if not s:
        return s

    # URL / CJK spacing (same as step_executor._sanitize_step)
    _url = r'a-zA-Z0-9._~:/?#\[\]@!$&\'()*+,;%=<>-'
    s = re.sub(rf'(https?://[{_url}]+)([一-鿿])', r'\1 \2', s)
    s = re.sub(rf'([一-鿿])(https?://)', r'\1 \2', s)

    # 「单位下拉框选择【汉东省院】」→「在【单位】中选择【汉东省院】」
    m = re.match(rf"^(.+?){_CTRL_TYPE_SUFFIX}\s*(?:中)?\s*(?:选择|选)\s*【([^】]+)】\s*$", s)
    if m:
        field, opt = m.group(1).strip(" ：:的"), m.group(2).strip()
        if field and opt:
            return f"在【{field}】中选择【{opt}】"

    # 「在【单位】下拉中选择【汉东】」→ keep Instant form without type word in 【】
    m = re.match(
        rf"^(?:在\s*)?【([^】]+?)】\s*(?:的)?{_CTRL_TYPE_SUFFIX}\s*(?:中)?\s*(?:选择|选)\s*【([^】]+)】\s*$",
        s,
    )
    if m:
        return f"在【{m.group(1).strip()}】中选择【{m.group(2).strip()}】"

    # 「点击【单位下拉框】」→「点击【单位】」
    m = re.match(rf"^点击【(.+?){_CTRL_TYPE_SUFFIX}】\s*$", s)
    if m:
        return f"点击【{m.group(1).strip()}】"

    # 「点击登录按钮」→「点击【登录】」
    m = re.match(rf"^点击\s*(.+?){_CTRL_TYPE_SUFFIX}\s*$", s)
    if m and "【" not in s:
        label = m.group(1).strip(" ：:的")
        if label:
            return f"点击【{label}】"

    # 「点击登录」/「点击 提交」（无【】、无控件类型词）→「点击【登录】」
    m = re.match(r"^点击\s*([^\s【】\[\]（）()]{1,40})\s*$", s)
    if m and "【" not in s:
        label = m.group(1).strip(" ：:的")
        if label and not re.search(r"(?:页面|完成|加载|等待)", label):
            return f"点击【{label}】"

    # 「密码输入【Abc12345】」/「用户名输入【admin】」—— 值误放进【】
    m = re.match(
        rf"^(?:在\s*)?(.+?)(?:{_CTRL_TYPE_SUFFIX})?\s*(?:输入|填写|填入)\s*【([^】]+)】\s*$",
        s,
    )
    if m:
        field, value = m.group(1).strip(" ：:的"), m.group(2).strip()
        # Avoid treating whole sentence as field when already Instant
        if field and value and "【" not in field:
            field = re.sub(rf"{_CTRL_TYPE_SUFFIX}$", "", field).strip(" ：:的")
            if field:
                return f"在【{field}】输入 {value}"

    # 「在【密码】输入【Abc12345】」→「在【密码】输入 Abc12345」
    m = re.match(
        r"^(?:在\s*)?【([^】]+)】\s*(?:中)?\s*(?:输入|填写|填入)\s*【([^】]+)】\s*$",
        s,
    )
    if m:
        return f"在【{m.group(1).strip()}】输入 {m.group(2).strip()}"

    # 「在用户名输入框输入 admin」→「在【用户名】输入 admin」
    m = re.match(
        rf"^(?:在\s*)?(.+?){_CTRL_TYPE_SUFFIX}\s*(?:中)?\s*(?:输入|填写|填入)\s*(.+)$",
        s,
    )
    if m and "【" not in s:
        field, value = m.group(1).strip(" ：:的"), m.group(2).strip()
        if field and value:
            return f"在【{field}】输入 {value}"

    # 「密码输入 Abc12345」/「输入密码 Abc12345」（常见字段名，无控件词）
    m = re.match(
        rf"^(?:在\s*)?({_FIELD_HINT})\s*(?:输入|填写|填入)\s*(.+)$",
        s,
    )
    if m and "【" not in s:
        return f"在【{m.group(1).strip()}】输入 {m.group(2).strip()}"
    m = re.match(
        rf"^(?:输入|填写|填入)\s*({_FIELD_HINT})\s*[：: ]\s*(.+)$",
        s,
    )
    if m and "【" not in s:
        return f"在【{m.group(1).strip()}】输入 {m.group(2).strip()}"

    # Strip control-type suffixes stuck inside existing【】
    def _strip_ctrl_in_brackets(match: re.Match) -> str:
        inner = match.group(1)
        cleaned = re.sub(rf"{_CTRL_TYPE_SUFFIX}$", "", inner).strip()
        return f"【{cleaned or inner}】"

    return re.sub(r"【([^】]+)】", _strip_ctrl_in_brackets, s)
