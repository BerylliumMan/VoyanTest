"""US4 单测：textGone 负向等待（先 RED）。

覆盖：
- step_normalize：「等待【X】消失/隐藏/不再出现」识别与回渲染
- coerce_structured_step 保留 wait_mode
- mcp_args：wait_mode=gone → {textGone}；出现 → {text}；秒数 → {time}；互斥（不出现 text/textGone 并存）
"""
from __future__ import annotations


def test_parse_wait_gone_variants():
    from core.step_normalize import parse_instant_to_structured

    for text in ("等待【加载中】消失", "等待【加载中】隐藏", "等待【加载中】不再出现"):
        parsed = parse_instant_to_structured(text)
        assert parsed is not None, text
        assert parsed["action"] == "wait"
        assert parsed["value"] == "加载中"
        assert parsed.get("wait_mode") == "gone", text


def test_parse_wait_appear_unchanged():
    from core.step_normalize import parse_instant_to_structured

    parsed = parse_instant_to_structured("等待【商品列表】出现")
    assert parsed["action"] == "wait"
    assert parsed["value"] == "商品列表"
    assert parsed.get("wait_mode") in (None, "text")


def test_render_wait_gone():
    from core.step_normalize import render_structured_step

    out = render_structured_step({
        "action": "wait", "target_name": "加载中", "value": "加载中", "wait_mode": "gone",
    })
    assert out == "等待【加载中】消失"

    out2 = render_structured_step({
        "action": "wait", "target_name": "商品列表", "value": "商品列表",
    })
    assert out2 == "等待【商品列表】出现"


def test_coerce_preserves_wait_mode():
    from core.step_normalize import coerce_structured_step

    out = coerce_structured_step({
        "action": "wait", "target_name": "加载中", "value": "加载中", "wait_mode": "gone",
    })
    assert out is not None
    assert out.get("wait_mode") == "gone"


def test_build_wait_gone_args():
    from core.mcp_args import build_mcp_args

    args = build_mcp_args("wait", selector="", value="加载中", wait_mode="gone")
    assert args == {"textGone": "加载中"}
    assert "text" not in args

    args2 = build_mcp_args("wait", selector="", value="商品列表")
    assert args2 == {"text": "商品列表"}

    args3 = build_mcp_args("wait", selector="", value="2")
    assert args3 == {"time": 2}

    args4 = build_mcp_args("wait", selector="", value="2", wait_mode="gone")
    assert args4 == {"time": 2}  # 纯秒数不因 gone 改变语义
