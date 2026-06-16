# core/runner/__init__.py
"""测试执行引擎 — 拆分为专注的小模块。

公共 API:
    run_test_case          – 单用例完整执行（自动创建/销毁浏览器）
    run_batch_test_cases   – 批量用例在共享浏览器中顺序执行
    run_test_case_in_browser – 在已有浏览器中执行单用例
    save_run_results       – 将执行结果持久化到数据库
"""

from core.runner._execution import run_test_case_in_browser
from core.runner._persistence import save_run_results
from core.runner._orchestrator import run_test_case, run_batch_test_cases

__all__ = [
    "run_test_case",
    "run_batch_test_cases",
    "run_test_case_in_browser",
    "save_run_results",
]
