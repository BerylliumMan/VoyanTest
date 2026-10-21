"""039：参数化固化脚本辅助（占位符 → 运行时 params 注入）。

脚本中所有含 `{{name}}` 的字符串字面量在生成期被包成 `_vt("...")`，入口函数
接收 `params` 并写入全局 `_VT_PARAMS`；回放时客户端按组传入数据行。
"""
from __future__ import annotations

import re

PARAM_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
_LITERAL_RE = re.compile(r'"(?:[^"\\\n]|\\.)*\{\{\s*[A-Za-z_][A-Za-z0-9_]*\s*\}\}(?:[^"\\\n]|\\.)*"')
_ENTRY_RE = re.compile(r"async\s+def\s+(test_case_\d+)\s*\(([^)]*)\)(?:\s*->[^:]+)?:")
_PRELUDE = '''import re
import os as _vt_os
import json as _vt_json


_VT_PARAMS: dict = _vt_json.loads(_vt_os.environ.get("VT_SCRIPT_PARAMS", "{}") or "{}")


def _vt(text: str) -> str:
    return re.sub(r"\\{\\{\\s*([A-Za-z_][A-Za-z0-9_]*)\\s*\\}\\}",
                  lambda m: str(_VT_PARAMS.get(m.group(1), m.group(0))), str(text))
'''


def script_uses_parameters(script: str) -> bool:
    return bool(PARAM_RE.search(script or ""))


def parameterize_script(script: str, *, case_id: int | None = None) -> str:
    """把脚本改为接收 ``params`` 的入口 + 字面量占位符替换。"""
    if not script:
        return script
    out = script
    # 先包裹业务字面量（不要包裹后面注入的 _vt 预置代码）
    def _wrap(match: re.Match) -> str:
        if out[max(0, match.start() - 4):match.start()] == "_vt(":
            return match.group(0)
        return f"_vt({match.group(0)})"

    out = _LITERAL_RE.sub(_wrap, out)

    if "_VT_PARAMS" not in out:
        _future = re.match(r"from __future__ import annotations\n", out)
        idx = _future.end() if _future else 0
        out = out[:idx] + _PRELUDE + "\n" + out[idx:]

    def _entry(match: re.Match) -> str:
        name, args = match.group(1), match.group(2).strip()
        if "params" in args:
            new_args = args
        elif args:
            new_args = f"{args}, params=None"
        else:
            new_args = "params=None"
        return (
            f"async def {name}({new_args}):\n"
            "    global _VT_PARAMS\n"
            "    _VT_PARAMS = params or {}"
        )

    out, _ = _ENTRY_RE.subn(_entry, out, count=1)
    return out
