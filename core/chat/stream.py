# core/chat/stream.py
# 034 对话流式输出：决策 JSON 的部分（整体）字符串提取。
#
# 契约：模型输出的完整文本是 {"thinking": "...", "reply": "...", "actions": [...]} 的 JSON；
# 流式场景下文本可能被任意截断（含转义序列中间）。本模块做容错的部分提取：
#   - 已解码内容是"单调增长的前缀"，编排层按增量切片推送 WS；
#   - 不完整转义（\uXXXX 半截 / 尾部反斜杠）处停止，等待后续增量；
#   - 最终仍以完整文本的正式解析（contracts.parse_decision）为准。
from __future__ import annotations

_ESCAPES = {
    "n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f",
    '"': '"', "\\": "\\", "/": "/",
}


def decode_partial_string(raw: str) -> str:
    """尽力解码一个可能被截断的 JSON 字符串体（不含两侧引号）。"""
    out: list[str] = []
    i = 0
    n = len(raw)
    while i < n:
        ch = raw[i]
        if ch != "\\":
            out.append(ch)
            i += 1
            continue
        if i + 1 >= n:
            break  # 尾部孤立反斜杠：等待后续增量
        nxt = raw[i + 1]
        if nxt == "u":
            if i + 6 > n:
                break  # \uXXXX 不完整
            try:
                out.append(chr(int(raw[i + 2 : i + 6], 16)))
            except ValueError:
                break  # 非法转义：停止（正式解析兜底）
            i += 6
            continue
        if nxt in _ESCAPES:
            out.append(_ESCAPES[nxt])
            i += 2
            continue
        out.append(nxt)  # 未知转义按字面输出
        i += 2
    return "".join(out)


def extract_partial_string(buffer: str, key: str) -> str:
    """从（可能不完整的）JSON 文本中提取字符串键 ``key`` 的当前值；找不到返回空串。"""
    needle = '"%s"' % key
    start = buffer.find(needle)
    if start < 0:
        return ""
    i = start + len(needle)
    n = len(buffer)
    while i < n and buffer[i] in " \t\r\n":
        i += 1
    if i >= n or buffer[i] != ":":
        return ""
    i += 1
    while i < n and buffer[i] in " \t\r\n":
        i += 1
    if i >= n or buffer[i] != '"':
        return ""
    i += 1
    j = i
    while j < n:
        ch = buffer[j]
        if ch == "\\":
            j += 2
            continue
        if ch == '"':
            break
        j += 1
    return decode_partial_string(buffer[i : min(j, n)])


class PartialDecisionStream:
    """累积流式文本，实时给出 thinking / reply 的部分值。"""

    def __init__(self) -> None:
        self.buffer = ""

    def feed(self, delta: str) -> None:
        self.buffer += delta or ""

    def get(self, key: str) -> str:
        return extract_partial_string(self.buffer, key)
