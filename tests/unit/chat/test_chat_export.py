"""034 US7：对话导出 Markdown —— 消息/时间/产物摘要一致性（T053）。"""
from types import SimpleNamespace

from app.routers.chat.evidence import conversation_to_markdown


class FakeConv:
    id = 7
    title = "登录用例生成"
    project_id = 30
    created_at = None


class FakeMsg:
    def __init__(self, role, content, refs=None, cards=None, created_at=None):
        self.role = role
        self.content = content
        self.refs = refs
        self.cards = cards
        self.created_at = created_at


def test_export_markdown_contains_messages_and_cards():
    messages = [
        FakeMsg("user", "帮我生成一条登录用例", created_at=SimpleNamespace(isoformat=lambda: "2026-10-10T10:00:00")),
        FakeMsg("assistant", "已生成草稿", created_at=SimpleNamespace(isoformat=lambda: "2026-10-10T10:00:05"),
                cards=[{"type": "draft_set", "case_kind": "ui", "status": "imported", "items": [{"title": "登录成功"}, {"title": "登录失败"}]}]),
        FakeMsg("user", "把第 1 步改一下", refs=[{"type": "case", "id": 662, "label": "登录成功"}, {"type": "file", "file_ref": "x", "label": "req.md"}]),
        FakeMsg("assistant", "对照已生成", cards=[
            {"type": "diff", "case_id": 662, "status": "pending", "changes": [{}, {}]},
            {"type": "run", "batch_id": 456, "status": "failed", "result": {"passed": 0, "failed": 1}},
            {"type": "evidence", "batch_id": 456, "status": "ready"},
        ]),
    ]
    md = conversation_to_markdown(FakeConv(), messages)
    assert "# 对话记录：登录用例生成" in md
    assert "对话 ID：7" in md
    assert "## 用户 · 2026-10-10T10:00:00" in md
    assert "帮我生成一条登录用例" in md
    assert "[草稿集] ui · 2 条 · 状态 imported" in md and "- 登录失败" in md
    assert "> 引用：case#662 登录成功" in md
    assert "file#" not in md  # 附件引用不导出（不落明文路径细节）
    assert "[修改对照] 用例 #662 · 状态 pending · 2 处变更" in md
    assert "[执行批次] #456 · 状态 failed · 通过 0 失败 1" in md
    assert "[失败归因] 批次 #456 · 状态 ready" in md


def test_export_markdown_empty_conversation():
    md = conversation_to_markdown(FakeConv(), [])
    assert "# 对话记录" in md and "##" not in md
