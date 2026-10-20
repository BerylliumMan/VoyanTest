"""037+ 探索报告材料：模块必须按业务页面分组（用户实测回归）。

问题：材料把「生成要求 / 已观察页面 / 主要功能点」写成 markdown 标题，
生成管线按标题切模块 → 用例模块显示成这些元信息。
"""
from core.chat.browse import build_report_materials


def _report(**kw):
    base = {
        "started_url": "http://example.test",
        "summary": "登录成功，观察了函件录入与事端处理",
        "pages": [
            {"url": "http://example.test/login", "title": "登录页"},
            {"url": "http://example.test/letter", "title": "函件录入"},
            {"url": "http://example.test/incident", "title": "事端处理"},
        ],
        "features": [],
    }
    base.update(kw)
    return base


def test_materials_use_explicit_business_modules():
    md = build_report_materials(_report(
        features=["函件录入可新增", "事端处理可查询"],
        feature_items=[
            {"module": "函件录入", "text": "点击新增打开弹窗"},
            {"module": "事端处理", "text": "按条件查询事件列表"},
        ],
    ))
    assert "## 函件录入" in md
    assert "## 事端处理" in md
    for bad in ("## 生成要求", "## 已观察页面", "## 主要功能点", "## 主要", "# UI"):
        assert bad not in md


def test_materials_infer_module_from_page_title_for_legacy_features():
    md = build_report_materials(_report(
        features=["函件录入支持多条件搜索", "登录页含账号密码输入框"],
    ))
    assert "## 函件录入" in md
    assert "## 登录页" in md
    # 无归属的功能点进入「通用功能」而不是元信息标题
    for bad in ("## 生成要求", "## 已观察页面", "## 主要功能点"):
        assert bad not in md


def test_materials_single_page_falls_back_to_its_title():
    md = build_report_materials(_report(
        pages=[{"url": "http://example.test/login", "title": "登录页"}],
        features=["页面有用户名和密码输入框"],
    ))
    assert "## 登录页" in md
    assert "## 通用功能" not in md


def test_materials_without_features_still_use_page_modules():
    md = build_report_materials(_report(features=[]))
    assert "## 登录页" in md
    assert "## 函件录入" in md
    assert "## 主要功能点" not in md
    assert "## 已观察页面" not in md


def test_materials_meta_is_plain_text_not_heading():
    md = build_report_materials(_report(features=["函件录入可新增"], feature_items=[
        {"module": "函件录入", "text": "点击新增打开弹窗"},
    ]))
    first = md.splitlines()[0]
    assert not first.startswith("#")
    assert "生成要求（以下为元信息" in md
    # 不能出现以【开头的要求块（会被 heading 正则当成模块）
    assert "【生成要求" not in md


def test_normalize_draft_module_replaces_meta_with_business_name():
    from app.services.chat_gen import _normalize_draft_module

    assert _normalize_draft_module("生成要求", ["1. 打开【函件录入】页面"]) == "函件录入"
    assert _normalize_draft_module("已观察页面", ["打开【事端处理】页面"]) == "事端处理"
    assert _normalize_draft_module("主要", ["打开【仪表盘】页面"]) == "仪表盘"
    assert _normalize_draft_module("", ["打开【函件录入】页面"]) == "函件录入"
    assert _normalize_draft_module("订单管理", []) == "订单管理"
    assert _normalize_draft_module("", []) == "通用功能"
    assert _normalize_draft_module("", ["打开【页面探索观察报告】"]) == "通用功能"
