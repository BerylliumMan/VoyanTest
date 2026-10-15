"""035 内置工具（导入即注册到全局 REGISTRY）。"""
from core.tools.builtin import actions  # noqa: F401 —— 7 能力工具化（US1）
from core.tools.builtin import demo  # noqa: F401 —— 扩展点演示工具（US5）
from core.tools.builtin import queries  # noqa: F401 —— 只读查询（US2）
