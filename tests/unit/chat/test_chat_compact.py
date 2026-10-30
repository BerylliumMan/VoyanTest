"""044 US1：三层上下文压缩（先红）。"""

from core.chat.compact import CompactPolicy, microcompact, select_for_summary


def test_policy_threshold_matches_zcode_formula():
    p = CompactPolicy(context_window=200_000, max_output_tokens=32_000)
    assert p.threshold == 166_000


def test_microcompact_clears_only_old_tool_results():
    msgs = [{"role": "system", "content": "s"}]
    for i in range(8):
        msgs.append({"role": "assistant", "content": "", "tool_calls": [{"id": f"c{i}"}]})
        msgs.append({"role": "tool", "tool_call_id": f"c{i}", "content": "x" * 2000})
    out = microcompact(msgs, keep_recent=2)
    assert out[-1]["content"] == msgs[-1]["content"]      # 最近工具结果保留
    assert "已清理" in out[2]["content"]                     # 较早被清理
    assert out[0]["content"] == "s"                        # system 不动


def test_select_for_summary_preserves_recent_assistant_group():
    msgs = [
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
        {"role": "tool", "content": "t1"},
        {"role": "user", "content": "u2"},
        {"role": "assistant", "content": "a2"},
        {"role": "tool", "content": "t2"},
    ]
    older, recent = select_for_summary(msgs, preserve_groups=1)
    assert [m["content"] for m in recent] == ["u2", "a2", "t2"]
    assert len(older) == 3
