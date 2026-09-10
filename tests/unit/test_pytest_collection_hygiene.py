"""收集阶段健壮性补丁的回归测试（补丁在仓库根 conftest.py）。

背景：仓库根存在跨 uid 不可读的软链（``.codegraph`` → 权限 700 的 ``~/.omo/...``）。
pytest 的 ``_IGNORED_ERRORS`` 原本只有 ``(ENOENT, ENOTDIR, EBADF, ELOOP)``，
EACCES 未被忽略 → ``PermissionError`` 从条目类型探测处抛出，整个收集阶段失败。
"""

from __future__ import annotations

import errno
import types

import _pytest.pathlib as _pathlib


def test_eacces_is_ignored_by_pytest_scandir():
    """补丁必须生效：EACCES 进入忽略列表。"""
    assert errno.EACCES in _pathlib._IGNORED_ERRORS


class _FakeIterator:
    def __init__(self, entries):
        self._entries = entries

    def __enter__(self):
        return iter(self._entries)

    def __exit__(self, *exc):
        return False


class _Entry:
    def __init__(self, name: str, *, denied: bool = False) -> None:
        self.name = name
        self.path = f"/fake/{name}"
        self._denied = denied

    def is_file(self) -> bool:
        if self._denied:
            raise PermissionError(errno.EACCES, "Permission denied", self.path)
        return True


class _UnreadableEntry(_Entry):
    """模拟跨 uid 不可读的软链条目。"""

    def __init__(self, name: str) -> None:
        super().__init__(name, denied=True)


def test_scandir_skips_unreadable_entry_instead_of_raising(monkeypatch):
    entries = [_UnreadableEntry(".codegraph"), _Entry("test_ok.py")]
    monkeypatch.setattr(
        _pathlib, "os", types.SimpleNamespace(scandir=lambda _p: _FakeIterator(entries))
    )

    scanned = _pathlib.scandir("/fake")

    # 不可读条目被跳过，可读条目保留 —— 不再抛 PermissionError
    assert [e.name for e in scanned] == ["test_ok.py"]


def test_scandir_still_raises_for_unexpected_oserror(monkeypatch):
    """非权限类异常仍要抛出，避免静默掩盖真实问题。"""

    class _BrokenEntry(_Entry):
        def is_file(self) -> bool:
            raise OSError(errno.EIO, "I/O error", self.path)

    monkeypatch.setattr(
        _pathlib,
        "os",
        types.SimpleNamespace(
            scandir=lambda _p: _FakeIterator([_BrokenEntry("weird")])
        ),
    )

    import pytest

    with pytest.raises(OSError):
        _pathlib.scandir("/fake")
