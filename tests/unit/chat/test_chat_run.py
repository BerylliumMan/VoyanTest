"""034 US4：执行证据提取 / 卡片状态映射 / 发起守卫 / watcher（T036/T037）。"""
import asyncio

import pytest

from app.services import chat_run
from tests.unit.chat.conftest import FakeConversation, FakeMessage


# ----------------------------
# 纯函数
# ----------------------------

def test_build_failure_summary_extracts():
    steps = [
        {"success": True, "description": "打开页面", "screenshot_path": None},
        {"success": False, "description": "断言失败: 期望 equals 200，实际为 401", "screenshot_path": "/tmp/s1.png"},
        {"success": False, "description": "[console] TypeError: x is not a function", "screenshot_path": None},
    ]
    s = chat_run.build_failure_summary(steps)
    assert s["failed_count"] == 1
    assert s["step"].startswith("断言失败")
    assert s["console"] == ["[console] TypeError: x is not a function"]
    assert s["screenshot_url"] == "/tmp/s1.png"


def test_build_failure_summary_console_cap_and_no_fail():
    steps = [{"success": True, "description": f"[console] e{i}"} for i in range(30)]
    s = chat_run.build_failure_summary(steps, console_max=5)
    assert s["failed_count"] == 0 and s["step"] is None and len(s["console"]) == 5


def test_split_log_segments_and_merged_run_log():
    merged = (
        "AgentRunner 完成: status=failed, turns=5, error=连续多次操作失败，可能目标无法达成"
        " | [console] [ERROR] Failed to load resource: 401"
    )
    segs = chat_run.split_log_segments(merged)
    assert len(segs) == 2
    steps = [
        {"success": "[console]" in seg, "description": seg, "screenshot_path": None}
        for seg in segs
    ]
    s = chat_run.build_failure_summary(steps)
    assert s["step"].startswith("AgentRunner 完成")
    assert len(s["console"]) == 1 and s["console"][0].startswith("[console] [ERROR]")
    assert chat_run.split_log_segments("") == []


def test_card_status_from_batch_mapping():
    assert chat_run.card_status_from_batch("running") == "running"
    assert chat_run.card_status_from_batch("passed") == "done"
    assert chat_run.card_status_from_batch("partial") == "done"
    assert chat_run.card_status_from_batch("failed") == "failed"
    assert chat_run.card_status_from_batch("cancelled") == "canceled"
    assert chat_run.card_status_from_batch("unknown") == "running"


# ----------------------------
# 守卫
# ----------------------------

@pytest.mark.asyncio
async def test_start_run_requires_project(monkeypatch):
    conv = FakeConversation(project_id=None)
    card, err, _ = await chat_run.start_run(
        None, conversation=conv, case_ids=[1], mode="server", agent_id=None, user_id=1,
    )
    assert card is None and "未绑定项目" in err


@pytest.mark.asyncio
async def test_start_run_parallel_cap(monkeypatch):
    conv = FakeConversation(project_id=30)

    async def active(db, cid):
        return 3

    monkeypatch.setattr(chat_run, "_active_run_count", active)
    card, err, _ = await chat_run.start_run(
        None, conversation=conv, case_ids=[1], mode="server", agent_id=None, user_id=1,
    )
    assert card is None and "并行执行已达上限" in err


@pytest.mark.asyncio
async def test_start_run_rejects_foreign_case(monkeypatch):
    conv = FakeConversation(project_id=30)

    async def active(db, cid):
        return 0

    class Case:
        id = 5
        project_id = 99
        name = "别家用例"

    async def get_tc(db, cid):
        return Case()

    monkeypatch.setattr(chat_run, "_active_run_count", active)
    monkeypatch.setattr(chat_run.crud, "get_test_case", get_tc)
    card, err, _ = await chat_run.start_run(
        None, conversation=conv, case_ids=[5], mode="server", agent_id=None, user_id=1,
    )
    assert card is None and "不属于当前对话" in err


@pytest.mark.asyncio
async def test_start_run_client_requires_agent(monkeypatch):
    conv = FakeConversation(project_id=30)

    async def active(db, cid):
        return 0

    class Case:
        id = 5
        project_id = 30
        name = "本家用例"

    async def get_tc(db, cid):
        return Case()

    monkeypatch.setattr(chat_run, "_active_run_count", active)
    monkeypatch.setattr(chat_run.crud, "get_test_case", get_tc)
    card, err, _ = await chat_run.start_run(
        None, conversation=conv, case_ids=[5], mode="client", agent_id=None, user_id=1,
    )
    assert card is None and "需要指定 Agent" in err


@pytest.mark.asyncio
async def test_start_run_happy_path_builds_card(monkeypatch):
    conv = FakeConversation(project_id=30)

    class Case:
        def __init__(self, i, name):
            self.id = i
            self.project_id = 30
            self.name = name

    async def active(db, cid):
        return 0

    async def get_tc(db, cid):
        return Case(cid, f"用例{cid}")

    async def pick_env(db, pid):
        return 17

    class User:
        id = 7

    async def load_user(db, uid):
        return User()

    async def invoke(db, *, user, case_ids, mode, agent_name, environment_id):
        assert mode == "server" and environment_id == 17
        return 456

    async def fake_watch(cid, bid, **kw):
        return None

    monkeypatch.setattr(chat_run, "_active_run_count", active)
    monkeypatch.setattr(chat_run.crud, "get_test_case", get_tc)
    monkeypatch.setattr(chat_run, "_pick_environment", pick_env)
    monkeypatch.setattr(chat_run, "_load_user", load_user)
    monkeypatch.setattr(chat_run, "_invoke_batch", invoke)
    monkeypatch.setattr(chat_run, "_watch_run", fake_watch)
    monkeypatch.setattr(chat_run.asyncio, "create_task", lambda coro: coro.close())

    card, err, task_id = await chat_run.start_run(
        None, conversation=conv, case_ids=[11, 12], mode="server", agent_id=None, user_id=7,
    )
    assert err == "" and task_id == "456"
    assert card["batch_id"] == 456 and card["status"] == "running"
    assert card["progress"] == {"total": 2, "done": 0}
    assert card["case_titles"] == ["用例11", "用例12"]


# ----------------------------
# watcher
# ----------------------------

@pytest.mark.asyncio
async def test_watch_run_updates_card_until_terminal(monkeypatch):
    conv_id = 3
    batch_id = 456
    msg = FakeMessage(conversation_id=conv_id, role="assistant", cards=[
        {"type": "run", "batch_id": batch_id, "status": "running", "case_ids": [11],
         "progress": {"total": 1, "done": 0}},
    ])
    writes = []

    class Batch:
        def __init__(self):
            self.id = batch_id
            self.status = "running"
            self.total_cases = 1
            self.passed = 0
            self.failed = 0

    batch = Batch()
    state = {"i": 0}

    async def fake_sleep(sec):
        state["i"] += 1
        if state["i"] == 2:
            batch.status = "passed"
            batch.passed = 1

    async def get_batch(db, bid):
        return batch

    async def find_card(db, cid, bid):
        return msg, msg.cards[0]

    async def write_card(db, m, card):
        writes.append(card)
        m.cards = [card]
        return m

    async def noop_emit(cid, ev):
        return None

    async def collect(db, bid, cids):
        return []

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
    monkeypatch.setattr(chat_run.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(chat_run.crud, "get_run_batch", get_batch)
    monkeypatch.setattr(chat_run, "_find_run_card_message", find_card)
    monkeypatch.setattr(chat_run, "_write_run_card", write_card)
    monkeypatch.setattr(chat_run, "_collect_evidence", collect)
    monkeypatch.setattr(chat_run, "emit", noop_emit)
    monkeypatch.setattr(chat_run, "logger", chat_run.logger)

    await chat_run._watch_run(conv_id, batch_id, max_iterations=10)
    assert len(writes) >= 2
    assert writes[-1]["status"] == "done"
    assert writes[-1]["result"] == {"passed": 1, "failed": 0}


@pytest.mark.asyncio
async def test_cancel_run_reuses_stop(monkeypatch):
    conv = FakeConversation(project_id=30)
    msg = FakeMessage(conversation_id=conv.id, role="assistant", cards=[
        {"type": "run", "batch_id": 456, "status": "running"},
    ])

    async def find_card(db, cid, bid):
        return msg, msg.cards[0]

    async def write_card(db, m, card):
        m.cards = [card]
        return m

    class User:
        id = 7

    async def load_user(db, uid):
        return User()

    stopped = {}

    async def fake_stop(batch_id, user=None, db=None):
        stopped["batch_id"] = batch_id
        return {"ok": True}

    import app.routers.report_router as rr

    monkeypatch.setattr(chat_run, "_find_run_card_message", find_card)
    monkeypatch.setattr(chat_run, "_write_run_card", write_card)
    monkeypatch.setattr(chat_run, "_load_user", load_user)
    monkeypatch.setattr(rr, "stop_batch", fake_stop)

    result, err = await chat_run.cancel_run(None, conversation=conv, batch_id=456, user_id=7)
    assert err == "" and stopped["batch_id"] == 456
    assert result["card"]["status"] == "canceled"

    # 已结束 → 拒绝
    result2, err2 = await chat_run.cancel_run(None, conversation=conv, batch_id=456, user_id=7)
    assert result2 is None and "已结束" in err2
