"""034 US2：生成服务辅助逻辑 / 串行守卫 / 导入错误路径（T021，先红）。"""
import os

import pytest

from app.services import chat_gen
from tests.unit.chat.conftest import FakeConversation, FakeMessage


def test_resolve_chat_file_guard(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    os.makedirs("data/chat_uploads/x", exist_ok=True)
    with open("data/chat_uploads/x/a.md", "w") as fh:
        fh.write("hi")
    assert chat_gen._resolve_chat_file("data/chat_uploads/x/a.md") is not None
    assert chat_gen._resolve_chat_file("data/chat_uploads/../secret.md") is None  # 路径穿越
    assert chat_gen._resolve_chat_file("/etc/passwd") is None
    assert chat_gen._resolve_chat_file("data/chat_uploads/x/missing.md") is None
    assert chat_gen._resolve_chat_file("") is None


@pytest.mark.asyncio
async def test_has_active_generation_guard(monkeypatch):
    conv = FakeConversation()
    running = FakeMessage(conversation_id=conv.id, role="assistant", cards=[
        {"type": "draft_set", "gen_session_id": "s1", "status": "generating", "items": []},
    ])
    done = FakeMessage(conversation_id=conv.id, role="assistant", cards=[
        {"type": "draft_set", "gen_session_id": "s0", "status": "draft", "items": []},
    ])

    async def list_after(db, cid, **kw):
        return [done]

    monkeypatch.setattr(chat_gen.chat_crud, "list_messages_after", list_after)
    assert await chat_gen._has_active_generation(None, conv.id) is False

    async def list_after2(db, cid, **kw):
        return [done, running]

    monkeypatch.setattr(chat_gen.chat_crud, "list_messages_after", list_after2)
    assert await chat_gen._has_active_generation(None, conv.id) is True


@pytest.mark.asyncio
async def test_generate_handler_rejects_when_active(monkeypatch):
    conv = FakeConversation()

    async def has_active(db, cid):
        return True

    monkeypatch.setattr(chat_gen, "_has_active_generation", has_active)
    outcome = await chat_gen.generate_handler(
        None, conv,
        {"type": "generate", "kind": "ui", "source": "text", "payload": {"description": "x"}},
        {"user_id": 7},
    )
    assert not outcome.ok and "已有生成任务进行中" in outcome.note


@pytest.mark.asyncio
async def test_import_drafts_error_paths(monkeypatch):
    conv = FakeConversation()

    async def find_card_none(db, cid, sid):
        return None, None

    monkeypatch.setattr(chat_gen, "_find_card_message", find_card_none)
    result, err = await chat_gen.import_drafts(None, conversation=conv, gen_session_id="s")
    assert result is None and "草稿集不存在" in err

    msg = FakeMessage(conversation_id=conv.id, role="assistant", cards=[
        {"type": "draft_set", "gen_session_id": "s1", "status": "draft", "items": []},
    ])

    async def find_card_ok(db, cid, sid):
        return msg, msg.cards[0]

    monkeypatch.setattr(chat_gen, "_find_card_message", find_card_ok)

    class Rec:
        status = "analyzing"
        case_kind = "ui"
        imported_count = 0

    async def get_session(db, sid):
        return Rec()

    monkeypatch.setattr(chat_gen.crud, "get_gen_session", get_session)
    result, err = await chat_gen.import_drafts(None, conversation=conv, gen_session_id="s1")
    assert result is None and "尚未完成" in err


# ----------------------------
# watcher（035 回归：卡片消息滞后落库）
# ----------------------------

@pytest.mark.asyncio
async def test_watch_generation_tolerates_late_card_message(monkeypatch):
    """035 回归：工具循环下卡片消息滞后落库时，生成 watcher 应等待而非早退。"""
    conv_id = 3
    session_id = "s-late"
    msg = FakeMessage(conversation_id=conv_id, role="assistant", cards=[
        {"type": "draft_set", "gen_session_id": session_id, "status": "generating", "items": []},
    ])
    writes = []
    state = {"i": 0}

    class Record:
        status = "completed"
        progress = 100
        progress_message = "分析完成"
        error_message = None

    async def fake_sleep(sec):
        state["i"] += 1

    async def get_gen_session(db, sid):
        return Record()

    async def find_card(db, cid, sid):
        if state["i"] < 3:
            return None, None
        return msg, msg.cards[0]

    async def list_gen_test_cases(db, sid):
        class TC:
            id = 1
            title = "用例A"
            test_steps = "打开页面\n点击登录"
            expected_result = "跳转仪表盘"
            priority = "P0"
            module = "登录"
        return [TC()]

    async def write_card(db, m, card):
        writes.append(card)
        m.cards = [card]
        return m

    async def noop_emit(cid, ev):
        return None

    class FakeSession:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *a):
            return False

    class FakeSessionLocal:
        def __call__(self):
            return FakeSession()

    import app.database as db_mod

    monkeypatch.setattr(db_mod, "AsyncSessionLocal", FakeSessionLocal())
    monkeypatch.setattr(chat_gen.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(chat_gen.crud, "get_gen_session", get_gen_session)
    monkeypatch.setattr(chat_gen.crud, "list_gen_test_cases", list_gen_test_cases)
    monkeypatch.setattr(chat_gen, "_find_card_message", find_card)
    monkeypatch.setattr(chat_gen, "_write_card", write_card)
    monkeypatch.setattr(chat_gen, "emit", noop_emit)

    await chat_gen._watch_generation(conv_id, session_id, max_iterations=10)
    assert len(writes) == 1
    assert writes[0]["status"] == "draft"
    assert writes[0]["items"][0]["title"] == "用例A"


@pytest.mark.asyncio
async def test_watch_generation_gives_up_after_grace(monkeypatch):
    conv_id = 3
    session_id = "s-never"
    state = {"finds": 0}

    class Record:
        status = "analyzing"
        progress = 10
        progress_message = ""
        error_message = None

    async def fake_sleep(sec):
        return None

    async def get_gen_session(db, sid):
        return Record()

    async def find_card(db, cid, sid):
        state["finds"] += 1
        return None, None

    async def noop_emit(cid, ev):
        return None

    class FakeSession:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *a):
            return False

    class FakeSessionLocal:
        def __call__(self):
            return FakeSession()

    import app.database as db_mod

    monkeypatch.setattr(db_mod, "AsyncSessionLocal", FakeSessionLocal())
    monkeypatch.setattr(chat_gen.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(chat_gen.crud, "get_gen_session", get_gen_session)
    monkeypatch.setattr(chat_gen, "_find_card_message", find_card)
    monkeypatch.setattr(chat_gen, "emit", noop_emit)

    await chat_gen._watch_generation(conv_id, session_id, max_iterations=100)
    assert state["finds"] == 60


@pytest.mark.asyncio
async def test_cleanup_completed_drafts_dedupes_filters_and_derives(monkeypatch):
    """2026-10-19 用户实测修复：探索生成草稿清理。

    重复合并（「打开首页」×N）/ 元信息噪声过滤（「页面探索观察报告」「起始站点」类
    报告字样）/ 空壳剔除 / 空标题派生。
    """
    from types import SimpleNamespace

    class TC:
        def __init__(self, id, title, steps):
            self.id = id
            self.title = title
            self.test_steps = steps

    tcs = [
        TC(1, "打开首页", "1. 打开首页"),
        TC(2, "打开首页", "1. 打开首页"),                          # 完全重复 → 删
        TC(3, "打开【页面探索观察报告】", "1. 打开【页面探索观察报告】"),  # 元信息噪声 → 删
        TC(4, "", "1. 按价格排序商品列表"),                          # 空标题 → 派生
        TC(5, "", ""),                                             # 空壳 → 删
        TC(6, "打开站内链接页面", "1. 打开【站内】\n2. 断言页面包含【站内链接】"),  # 探针章节名噪声 → 删
        TC(7, "查看近7天执行趋势", "1. 打开【探索过程】\n2. 断言图表可见"),  # 章节名污染 → 删
    ]
    deleted, updated = [], []

    async def list_tcs(db, sid):
        return list(tcs)

    async def delete_raw(db, row_id):
        deleted.append(row_id)
        return True

    async def update_raw(db, row_id, fields):
        updated.append((row_id, fields))
        return None

    monkeypatch.setattr(chat_gen.crud, "list_gen_test_cases", list_tcs)
    monkeypatch.setattr(chat_gen.crud, "delete_gen_test_case_raw", delete_raw)
    monkeypatch.setattr(chat_gen.crud, "update_gen_test_case_raw", update_raw)

    record = SimpleNamespace(id="s1", project_description="浏览器探索生成（会话 #1）")
    removed = await chat_gen._cleanup_completed_drafts(None, record)

    assert removed == 5
    assert deleted == [2, 3, 5, 6, 7]
    assert updated and updated[0][0] == 4 and updated[0][1]["title"] == "按价格排序商品列表"


@pytest.mark.asyncio
async def test_cleanup_keeps_meta_markers_for_normal_generation(monkeypatch):
    """普通（非探索）生成不套用元信息噪声过滤——「站内链接」可能是站点真实功能。"""
    from types import SimpleNamespace

    class TC:
        def __init__(self, id, title, steps):
            self.id = id
            self.title = title
            self.test_steps = steps

    tcs = [TC(1, "站内链接校验", "1. 打开站内链接页面\n2. 断言页面包含【站内链接】")]
    deleted = []

    async def list_tcs(db, sid):
        return list(tcs)

    async def delete_raw(db, row_id):
        deleted.append(row_id)
        return True

    monkeypatch.setattr(chat_gen.crud, "list_gen_test_cases", list_tcs)
    monkeypatch.setattr(chat_gen.crud, "delete_gen_test_case_raw", delete_raw)

    record = SimpleNamespace(id="s2", project_description="对话生成（会话 #1）")
    removed = await chat_gen._cleanup_completed_drafts(None, record)

    assert removed == 0 and deleted == []


@pytest.mark.asyncio
async def test_cleanup_near_duplicate_merge_browse(monkeypatch):
    """近重复合并（仅探索来源）：核心步骤等价/包含、标题相似度、空心用例。

    用 2026-10-19 实测样本（会话 76）构造：
    - 「执行报告批次对比结果查看」vs「执行报告顶部点击对比批次入口」：核心步骤等价 → 留厚者；
    - 「查看执行报告列表列与状态标签」⊂「执行报告列表字段展示」：包含 → 留厚者；
    - 「项目管理页项目列表展示」vs「打开项目管理并查看项目列表」：标题相似 → 合并；
    - 「全部通过状态标签展示」vs「全部失败状态标签展示」：不同断言 → 都保留；
    - 空心「顶栏语言入口切换界面语言」（仅 1 步打开首页）→ 删。
    """
    from types import SimpleNamespace

    class TC:
        def __init__(self, id, title, steps):
            self.id = id
            self.title = title
            self.test_steps = steps

    tcs = [
        TC(1, "执行报告批次对比结果查看", "1. 打开【执行报告】\n2. 点击【对比批次】\n3. 断言【对比批次】可见"),
        TC(2, "执行报告顶部点击对比批次入口", "1. 打开【执行报告】\n2. 点击【对比批次】\n3. 断言【对比批次】可见"),
        TC(3, "查看执行报告列表列与状态标签", "1. 打开【执行报告】\n2. 断言页面包含【批次】\n3. 断言页面包含【状态】"),
        TC(4, "执行报告列表字段展示", "1. 打开【执行报告列表】\n2. 断言页面包含【批次】\n3. 断言页面包含【项目】\n4. 断言页面包含【状态】"),
        TC(5, "项目管理页项目列表展示", "1. 打开【项目管理】\n2. 断言页面包含【项目管理】"),
        TC(6, "打开项目管理并查看项目列表", "1. 打开【项目管理】\n2. 断言【项目管理】可见"),
        TC(7, "全部通过状态标签展示", "1. 打开【执行报告列表】\n2. 等待【状态】出现\n3. 断言页面包含【全部通过】"),
        TC(8, "全部失败状态标签展示", "1. 打开【执行报告列表】\n2. 等待【状态】出现\n3. 断言页面包含【全部失败】"),
        TC(9, "顶栏语言入口切换界面语言", "1. 打开首页"),
    ]
    deleted = []

    async def list_tcs(db, sid):
        return [t for t in tcs if t.id not in deleted]

    async def delete_raw(db, row_id):
        deleted.append(row_id)
        return True

    async def update_raw(db, row_id, fields):
        return None

    monkeypatch.setattr(chat_gen.crud, "list_gen_test_cases", list_tcs)
    monkeypatch.setattr(chat_gen.crud, "delete_gen_test_case_raw", delete_raw)
    monkeypatch.setattr(chat_gen.crud, "update_gen_test_case_raw", update_raw)

    record = SimpleNamespace(id="s9", project_description="浏览器探索生成（会话 #9）")
    removed = await chat_gen._cleanup_completed_drafts(None, record)

    assert 9 in deleted          # 空心用例删
    assert 2 in deleted          # 核心步骤等价 → 留厚者（同长取最早=1）
    assert 3 in deleted          # 包含 → 留厚者 4
    assert 5 in deleted or 6 in deleted  # 标题相似 → 合并
    assert 7 not in deleted and 8 not in deleted  # 不同断言都保留
    assert removed == len(deleted) == 4


@pytest.mark.asyncio
async def test_cleanup_near_duplicate_not_applied_to_normal_source(monkeypatch):
    """普通（非探索）来源不套用近重复合并（文档生成的多条相似用例可能是刻意变体）。"""
    from types import SimpleNamespace

    class TC:
        def __init__(self, id, title, steps):
            self.id = id
            self.title = title
            self.test_steps = steps

    tcs = [
        TC(1, "项目管理页项目列表展示", "1. 打开【项目管理】\n2. 断言页面包含【项目管理】"),
        TC(2, "打开项目管理并查看项目列表", "1. 打开【项目管理】\n2. 断言【项目管理】可见"),
    ]
    deleted = []

    async def list_tcs(db, sid):
        return list(tcs)

    async def delete_raw(db, row_id):
        deleted.append(row_id)
        return True

    monkeypatch.setattr(chat_gen.crud, "list_gen_test_cases", list_tcs)
    monkeypatch.setattr(chat_gen.crud, "delete_gen_test_case_raw", delete_raw)

    record = SimpleNamespace(id="s10", project_description="对话生成（会话 #9）")
    removed = await chat_gen._cleanup_completed_drafts(None, record)
    assert removed == 0 and deleted == []
