from core.locator_candidates import (
    LocatorCandidate,
    actionable_candidates,
    extract_candidates,
    serialize_candidates,
    snapshot_has_visible_overlay,
    validate_candidate_ref,
)


def test_validate_candidate_ref_rejects_disabled_or_obscured_targets():
    candidates = (
        LocatorCandidate("e1", "button", "关闭", "关闭", {}, ()),
        LocatorCandidate("e2", "button", "提交", "提交", {}, (), enabled=False),
        LocatorCandidate("e3", "button", "保存", "保存", {}, (), obscured=True),
    )

    assert validate_candidate_ref(
        "e2", snapshot_version="s1", decision_version="s1", candidates=candidates
    ).failure_kind == "disabled"
    assert validate_candidate_ref(
        "e3", snapshot_version="s1", decision_version="s1", candidates=candidates
    ).failure_kind == "obscured"


def test_serialize_candidates_bounds_model_context():
    candidates = tuple(
        LocatorCandidate(f"e{i}", "button", str(i), str(i), {}, ())
        for i in range(3)
    )

    serialized = serialize_candidates(candidates, max_candidates=2)

    assert "ref=e0" in serialized
    assert "ref=e1" in serialized
    assert "ref=e2" not in serialized


def test_actionable_candidates_filters_invisible_disabled_and_obscured():
    candidates = (
        LocatorCandidate("e1", "button", "可见", "可见", {}, ()),
        LocatorCandidate("e2", "button", "隐藏", "隐藏", {}, (), visible=False),
        LocatorCandidate("e3", "button", "禁用", "禁用", {}, (), enabled=False),
        LocatorCandidate("e4", "button", "遮挡", "遮挡", {}, (), obscured=True),
    )

    assert [item.ref for item in actionable_candidates(candidates)] == ["e1"]


def test_extract_ref_token_recovers_pasted_candidate_line():
    from core.locator_candidates import extract_ref_token

    pasted = "ref=f3e24 role=textbox name='请选择单位' frame=main"
    assert extract_ref_token(pasted) == "f3e24"
    assert extract_ref_token("f3e24") == "f3e24"
    assert extract_ref_token("#submit-btn") == "#submit-btn"
    assert extract_ref_token(None) is None


def test_snapshot_lines_mark_hidden_and_disabled_candidates():
    candidates = extract_candidates(
        '- button "隐藏" [ref=e1 hidden]\n- button "禁用" [ref=e2 disabled]'
    )

    assert candidates[0].visible is False
    assert candidates[1].enabled is False
    assert snapshot_has_visible_overlay('- dialog "提示" [ref=e3]') is True


def test_extract_keeps_role_for_unnamed_elements():
    """无名称元素保留控件角色（2026-10-14：`- textbox [ref=e13]` 曾退化为 unknown，
    无标注表格表单页全部元素丢角色，模型无法识别输入框）。"""
    snap = (
        "### Snapshot\n"
        "- generic [active] [ref=e1]:\n"
        "  - textbox [ref=e13]\n"
        "  - cell \"用户名：\" [ref=e11]\n"
        "  - button \"登录\" [ref=e26] [cursor=pointer]\n"
        "  - img [ref=e22] [cursor=pointer]\n"
        "  - checkbox [checked] [ref=e30]\n"
        "  - link [ref=e31]\n"
    )
    got = {c.ref: (c.role, c.name) for c in extract_candidates(snap)}
    assert got["e13"] == ("textbox", "")
    assert got["e26"] == ("button", "登录")
    assert got["e22"][0] == "img"
    assert got["e30"][0] == "checkbox"
    assert got["e31"][0] == "link"
    assert got["e1"][0] == "generic"


def test_extract_colon_text_label_for_nonstandard_buttons():
    """div 按钮（非标准控件）：冒号后的可见文案必须成为候选标签。

    2026-10-19 实测复现：中文后台工具栏按钮为 ``<div class="btn">新增</div>``，
    快照行为 ``- generic [ref=f1e9] [cursor=pointer]: 新增``——此前标签被丢弃，
    候选渲染为空名，模型「截图里看得到按钮、候选里却找不到」。
    """
    snap = (
        '- generic [ref=f1e9] [cursor=pointer]: 新增\n'
        '- generic [ref=f1e10] [cursor=pointer]: "删除"\n'
        '- generic [ref=f1e11]: 导出\n'
        '- generic [ref=f1e12]:\n'
    )
    got = {c.ref: (c.role, c.name) for c in extract_candidates(snap)}
    assert got["f1e9"] == ("generic", "新增")
    assert got["f1e10"] == ("generic", "删除")
    assert got["f1e11"] == ("generic", "导出")
    assert got["f1e12"] == ("generic", "")  # 冒号后为空 → 保持空名，不误取
    # cursor=pointer 标记随行捕获（截断保护用）
    attrs = {c.ref: dict(c.attributes) for c in extract_candidates(snap)}
    assert attrs["f1e9"].get("cursor") == "pointer"
    assert "cursor" not in attrs["f1e11"]


def test_prioritize_candidates_keeps_controls_when_truncated():
    """候选超限截断时必须保住操作控件（2026-10-19：DOM 后置工具栏按钮 ref 排第 621，
    前 80 静默截断后模型完全看不到「新增」按钮）。"""
    from core.locator_candidates import prioritize_candidates

    links = tuple(
        LocatorCandidate(f"e{i}", "link", f"行{i}", f"行{i}", {}, ())
        for i in range(1, 201)
    )
    button = LocatorCandidate("f2e621", "button", "新增", "新增", {}, ())
    # div 按钮：generic + cursor=pointer（非标准控件也要保住）
    div_btn = LocatorCandidate(
        "f2e622", "generic", "导出", "导出", {"cursor": "pointer"}, ()
    )
    selected, omitted = prioritize_candidates(links + (button, div_btn), 80)

    assert len(selected) == 80
    assert omitted == 122
    refs = [c.ref for c in selected]
    assert "f2e621" in refs and "f2e622" in refs  # 后置按钮/div 按钮保住了
    # 保持原顺序输出（链接在前、按钮在后）
    assert refs[-2:] == ["f2e621", "f2e622"]
    # 未超限时原样返回
    same, none_omitted = prioritize_candidates(links[:10], 80)
    assert same == links[:10] and none_omitted == 0


def test_serialize_candidates_notice_when_truncated():
    from core.locator_candidates import prioritize_candidates  # noqa: F401

    links = tuple(
        LocatorCandidate(f"e{i}", "link", f"行{i}", f"行{i}", {}, ())
        for i in range(1, 6)
    )
    button = LocatorCandidate("e99", "button", "新增", "新增", {}, ())
    out = serialize_candidates(links + (button,), max_candidates=3)

    assert "ref=e99" in out  # 控件优先保住
    assert "另有 3 个候选未列出" in out  # 截断必须显式告知，不许静默


def test_extract_child_text_label_for_span_buttons():
    """span 包文本的按钮：标签在直接子文本节点上（第三种形态，2026-10-19 实测）。

    快照形态：`- generic [ref=e2]:` 下一行 `  - text: 新增`——此前空名，
    模型「看得到文本、找不到可点元素」。
    """
    snap = (
        "- generic [active] [ref=e1]:\n"
        "  - generic [ref=e2]:\n"
        "    - text: 新增\n"
        "  - link \"删除\" [ref=e3] [cursor=pointer]\n"
        "  - generic [ref=e9]:\n"
        "    - button \"X\" [ref=e10]\n"
    )
    got = {c.ref: (c.role, c.name) for c in extract_candidates(snap)}
    assert got["e2"] == ("generic", "新增")  # 子文本节点标签
    assert got["e1"] == ("generic", "")      # 容器不臆测（更深行里已有带 ref 元素）
    assert got["e9"] == ("generic", "")      # 首个子元素是带 ref 按钮 → 不贴标签


def test_extract_child_text_label_skips_container_with_sibling_refs():
    """容器更深块里还有其它可点元素时不贴标签（避免误导点击，交文案兜底）。"""
    snap = (
        "- generic [ref=e2]:\n"
        "  - text: 新增\n"
        "  - link \"删除\" [ref=e3] [cursor=pointer]\n"
    )
    got = {c.ref: (c.role, c.name) for c in extract_candidates(snap)}
    assert got["e2"] == ("generic", "")  # 容器不贴标签（块内有 ref）
    assert got["e3"] == ("link", "删除")
