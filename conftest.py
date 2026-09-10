"""仓库级 pytest 配置：收集阶段健壮性补丁。

## 为什么需要这个文件

仓库根目录会出现「指向工作区外、当前 uid 读不到」的符号链接，最典型的是
``.codegraph``（由 ``.omo`` codegraph 工具生成，经 ``.git/info/exclude`` 忽略）：
它指向 ``/home/lzl/.omo/codegraph/projects/<hash>``，而 ``~/.omo`` 的权限是
``drwx------``（仅属主可读）。只要运行 pytest 的 uid 不是该目录属主
（容器、CI、其它用户、agent 运行时），这个软链就成了「无法 stat 的条目」。

pytest 在 ``_pytest/pathlib.py::scandir()`` 里会为每个目录条目探测类型::

    for entry in s:
        try:
            entry.is_file()
        except OSError as err:
            if _ignore_error(err):
                continue          # 跳过该条目
            raise                 # 其它错误原样抛出

而 ``_IGNORED_ERRORS = (ENOENT, ENOTDIR, EBADF, ELOOP)`` **不含 EACCES**，
于是 ``PermissionError: [Errno 13] ... '.codegraph'`` 被重新抛出，收集阶段整体失败::

    ERROR: found no collectors for .../tests/unit/core/test_x.py
    ERROR collecting .
    E   PermissionError: [Errno 13] Permission denied: '.../.codegraph'

这个失败**无法**用 ``norecursedirs`` / ``--ignore`` / ``pytest_ignore_collect`` 规避：
异常发生在「条目类型探测」内部，上述钩子都在它之后才被调用；而且 pytest 会把参数
的祖先目录（含 rootdir）加入收集路径，因此 ``Dir(rootdir).collect()`` 必然扫到它。

## 修法

把 EACCES 并入 pytest 既有的忽略列表 —— 无法探测类型的条目被**跳过**，而不是炸掉
整个测试收集。这是 pytest 自带的扩展点；健康仓库上行为完全不变（不会跳过任何可读
条目），被跳过的条目会在配置阶段显式告警，避免静默掩盖权限问题。

## 为什么放在仓库根，而不是 tests/ 下

本文件**必须**待在仓库根，不要「整理」进 ``tests/``：

- ``.gitignore`` 里有 ``tests/``（来自最初的 ``d93f451``），``tests/`` 下 104 个 ``.py``
  只有 6 个被 git 跟踪。补丁放进 ``tests/`` 就不会进版本控制，CI / 新克隆上
  原问题照旧复现。
- pytest 在收集前会加载 rootdir 的 ``conftest.py``，所以放在根上对**所有**参数
  生效（不仅限于 ``tests/`` 下的路径）。

## 何时可以删

若上游把 EACCES 纳入 ``_IGNORED_ERRORS``，或本仓库不再出现跨 uid 不可读的根目录
条目，本补丁即可移除。
"""

from __future__ import annotations

import errno
import os

import _pytest.pathlib as _pathlib
import pytest

# ── 扩展 pytest 的忽略列表（import 时生效，早于收集阶段）────────────────────
if errno.EACCES not in _pathlib._IGNORED_ERRORS:
    _pathlib._IGNORED_ERRORS = (*_pathlib._IGNORED_ERRORS, errno.EACCES)


def _unreadable_root_entries(rootpath) -> list[str]:
    """返回仓库根下「因权限不足无法探测类型」的条目名。"""
    names: list[str] = []
    try:
        with os.scandir(rootpath) as entries:
            for entry in entries:
                try:
                    entry.is_file()
                except PermissionError:
                    names.append(entry.name)
                except OSError:
                    # 已在 pytest 忽略列表内（悬空/自引用软链等），无需告警
                    continue
    except OSError:
        return []
    return names


def pytest_configure(config: pytest.Config) -> None:
    """显式告警被跳过的条目，而不是静默隐藏权限问题。"""
    skipped = _unreadable_root_entries(config.rootpath)
    if not skipped:
        return
    config.issue_config_time_warning(
        pytest.PytestWarning(
            "跳过仓库根下无法读取的条目（权限不足，不参与收集）："
            + ", ".join(sorted(skipped))
        ),
        stacklevel=1,
    )
