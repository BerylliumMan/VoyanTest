"""038 US4：对话密码显示打码（显示/导出层）。"""
from __future__ import annotations

from core.chat.mask import mask_text


def test_mask_chinese_and_english():
    assert "Admin@2024" not in mask_text("登录，用户名 admin，密码 Admin@2024，查看功能")
    assert mask_text("密码：Abc123") == "密码：******"
    assert mask_text("password=secret") == "password=******"
    assert mask_text("PWD: xyz") == "PWD: ******"


def test_mask_multiple_occurrences():
    out = mask_text("密码 aaa 和 password: bbb")
    assert "aaa" not in out and "bbb" not in out
    assert out.count("******") == 2


def test_mask_does_not_touch_non_password_words():
    assert mask_text("打开密码框并输入") == "打开密码框并输入"
    assert mask_text("用户名 admin") == "用户名 admin"


def test_mask_url_param():
    out = mask_text("http://x/login?password=abc123&next=1")
    assert "abc123" not in out and "******" in out


def test_mask_empty():
    assert mask_text("") == ""
    assert mask_text(None) == ""
