"""US6 单测：browser_find 能力探测（先 RED）。

覆盖：
- caps 解析 / 缓存 / 未知处理
- 无 browser_find → 降级分支
- find 动作参数构造（text/regex 二选一）
"""
from __future__ import annotations
# 注：US6 的实际测试集中在本特性配套的 tests/unit/test_browser_probe_hidden.py
# （caps 缓存/映射/find 辅助），本文件保留为能力面占位说明。
