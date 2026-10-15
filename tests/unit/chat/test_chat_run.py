"""034 US4：执行证据提取 / 卡片状态映射 / 发起守卫 / watcher（T036/T037）。"""
import asyncio

import pytest

from app.services import chat_run
from tests.unit.chat.conftest import FakeConversation, FakeMessage


class Env:
    """轻量环境对象（替代 ORM Environment）。"""

    def __init__(self, id=17, name="默认环境", base_url="http://ok.example", project_id=30):
        self.id = id
        self.name = name
        self.base_url = base_url
        self.project_id = project_id
        self.is_default = False


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
    assert chat_run.card_status_from_batch("paused") == "paused"
    assert chat_run.card_status_from_batch("passed") == "done"
    assert chat_run.card_status_from_batch("partial") == "done"
    assert chat_run.card_status_from_batch("failed") == "failed"
    assert chat_run.card_status_from_batch("cancelled") == "canceled"
    assert chat_run.card_status_from_batch("unknown") == "running"


# ----------------------------
# 环境解析 / 活性提示（卡片体验修复）
# ----------------------------

def test_format_activity_success_and_failure():
    class TC:
        turn_number = 3
        tool_name = "goto"
        success = 1
        error_message = ""

    assert chat_run.format_activity(TC()) == "第 3 轮 · goto"

    class TC2:
        turn_number = 2
        tool_name = "click"
        success = 0
        error_message = "\x1b[2m### Error\nError: net::ERR_CONNECTION_REFUSED at http://x\x1b[22m"

    text = chat_run.format_activity(TC2())
    assert "第 2 轮 · click" in text
    assert "ERR_CONNECTION_REFUSED" in text
    assert "###" not in text
    assert "\x1b" not in text


def test_validate_action_run_case_environment_id():
    from core.chat.contracts import validate_action

    norm, err = validate_action({"type": "run_case", "case_ids": [1], "environment_id": 35})
    assert err == "" and norm["environment_id"] == 35
    norm, err = validate_action({"type": "run_case", "case_ids": [1]})
    assert err == "" and norm["environment_id"] is None
    assert norm["mode"] is None  # 未指定形态 → 由对话设置决定
    _, err = validate_action({"type": "run_case", "case_ids": [1], "environment_id": "abc"})
    assert "environment_id" in err
    _, err = validate_action({"type": "run_case", "case_ids": [1], "environment_id": -1})
    assert "environment_id" in err
    # 2026-10-15 修复：mode=client 未带 agent_id 合法（对话默认客户端会在处理器里补齐）
    norm, err = validate_action({"type": "run_case", "case_ids": [1], "mode": "client"})
    assert err == "" and norm["mode"] == "client" and norm["agent_id"] is None
    norm, err = validate_action({"type": "run_case", "case_ids": [1], "mode": "client", "agent_id": 3})
    assert err == "" and norm["mode"] == "client" and norm["agent_id"] == 3


@pytest.mark.asyncio
async def test_pick_environment_prefers_default(monkeypatch):
    envs = [Env(1, "a", "http://a"), Env(2, "b", "http://b")]
    envs[1].is_default = True

    async def get_envs(db, pid):
        return envs

    monkeypatch.setattr(chat_run.crud, "get_environments", get_envs)
    got = await chat_run._pick_environment(None, 30)
    assert got is envs[1]


@pytest.mark.asyncio
async def test_resolve_environment_guards(monkeypatch):
    class DB:
        def __init__(self, env):
            self.env = env

        async def get(self, model, id):
            return self.env

    # 环境不存在
    got, err = await chat_run._resolve_environment(DB(None), 30, 35)
    assert got is None and "不存在" in err

    # 环境不属于当前项目
    foreign = Env(35, "x", "http://x", project_id=99)
    got, err = await chat_run._resolve_environment(DB(foreign), 30, 35)
    assert got is None and "不属于" in err

    # 显式指定命中
    mine = Env(35, "034-smoke-local", "http://127.0.0.1:8002/login", project_id=30)
    got, err = await chat_run._resolve_environment(DB(mine), 30, 35)
    assert err == "" and got is mine


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

    async def resolve_env(db, pid, eid=None):
        assert eid is None
        return Env(17, "默认环境", "http://ok.example", project_id=30), ""

    async def probe(url):
        assert url == "http://ok.example"
        return True

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
    monkeypatch.setattr(chat_run, "_resolve_environment", resolve_env)
    monkeypatch.setattr(chat_run, "_probe_env_reachable", probe)
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
    assert card["environment_id"] == 17 and card["environment_name"] == "默认环境"
    assert card["environment_url"] == "http://ok.example"
    assert card["env_warning"] is None


@pytest.mark.asyncio
async def test_start_run_explicit_env_and_unreachable_warning(monkeypatch):
    conv = FakeConversation(project_id=30)
    calls = {}

    class Case:
        id = 11
        project_id = 30
        name = "用例11"

    async def active(db, cid):
        return 0

    async def get_tc(db, cid):
        return Case()

    async def resolve_env(db, pid, eid=None):
        calls["eid"] = eid
        return Env(35, "034-smoke-local", "http://127.0.0.1:9", project_id=30), ""

    async def probe(url):
        calls["probe"] = url
        return False

    class User:
        id = 7

    async def load_user(db, uid):
        return User()

    async def invoke(db, *, user, case_ids, mode, agent_name, environment_id):
        calls["invoke_env"] = environment_id
        return 456

    async def fake_watch(cid, bid, **kw):
        return None

    monkeypatch.setattr(chat_run, "_active_run_count", active)
    monkeypatch.setattr(chat_run.crud, "get_test_case", get_tc)
    monkeypatch.setattr(chat_run, "_resolve_environment", resolve_env)
    monkeypatch.setattr(chat_run, "_probe_env_reachable", probe)
    monkeypatch.setattr(chat_run, "_load_user", load_user)
    monkeypatch.setattr(chat_run, "_invoke_batch", invoke)
    monkeypatch.setattr(chat_run, "_watch_run", fake_watch)
    monkeypatch.setattr(chat_run.asyncio, "create_task", lambda coro: coro.close())

    card, err, _ = await chat_run.start_run(
        None, conversation=conv, case_ids=[11], mode="server", agent_id=None,
        user_id=7, environment_id=35,
    )
    assert err == ""
    assert calls["eid"] == 35 and calls["invoke_env"] == 35
    assert calls["probe"] == "http://127.0.0.1:9"
    assert card["environment_id"] == 35 and card["environment_name"] == "034-smoke-local"
    assert "不可达" in card["env_warning"]


@pytest.mark.asyncio
async def test_start_run_rejects_unknown_environment(monkeypatch):
    conv = FakeConversation(project_id=30)

    async def active(db, cid):
        return 0

    class Case:
        id = 11
        project_id = 30
        name = "用例11"

    async def get_tc(db, cid):
        return Case()

    async def resolve_env(db, pid, eid=None):
        return None, f"环境 #{eid} 不存在"

    monkeypatch.setattr(chat_run, "_active_run_count", active)
    monkeypatch.setattr(chat_run.crud, "get_test_case", get_tc)
    monkeypatch.setattr(chat_run, "_resolve_environment", resolve_env)

    card, err, _ = await chat_run.start_run(
        None, conversation=conv, case_ids=[11], mode="server", agent_id=None,
        user_id=7, environment_id=999,
    )
    assert card is None and "不存在" in err


@pytest.mark.asyncio
async def test_run_case_handler_note_includes_env(monkeypatch):
    async def fake_start(db, **kw):
        return {
            "type": "run", "batch_id": 1, "environment_name": "envA",
            "env_warning": "目标环境不可达",
        }, "", "1"

    monkeypatch.setattr(chat_run, "start_run", fake_start)
    outcome = await chat_run.run_case_handler(
        None, FakeConversation(project_id=30), {"case_ids": [1]}, {"user_id": 7},
    )
    assert outcome.ok and "envA" in outcome.note and "不可达" in outcome.note


@pytest.mark.asyncio
async def test_run_case_handler_uses_conversation_context(monkeypatch):
    """执行默认使用对话选择的环境与客户端；动作显式指定时覆盖。"""
    calls = {}

    async def fake_start(db, **kw):
        calls.clear()
        calls.update(kw)
        return {"type": "run", "batch_id": 9, "environment_name": "e1", "env_warning": None}, "", "9"

    monkeypatch.setattr(chat_run, "start_run", fake_start)

    # 1) 对话选了环境 35 + 客户端 26：动作什么都没说 → 用对话选择，形态自动 client
    conv = FakeConversation(project_id=30, environment_id=35, agent_id=26)
    outcome = await chat_run.run_case_handler(None, conv, {"case_ids": [1], "mode": None, "agent_id": None, "environment_id": None}, {"user_id": 7})
    assert outcome.ok
    assert calls["environment_id"] == 35 and calls["agent_id"] == 26 and calls["mode"] == "client"

    # 2) 对话选了客户端，但用户明确要求服务端 → 形态 server、不带 agent
    conv2 = FakeConversation(project_id=30, environment_id=35, agent_id=26)
    outcome = await chat_run.run_case_handler(None, conv2, {"case_ids": [1], "mode": "server", "agent_id": None, "environment_id": None}, {"user_id": 7})
    assert outcome.ok and calls["mode"] == "server" and calls["agent_id"] is None

    # 3) 动作显式指定环境 → 覆盖对话选择
    conv3 = FakeConversation(project_id=30, environment_id=35, agent_id=None)
    outcome = await chat_run.run_case_handler(None, conv3, {"case_ids": [1], "mode": None, "agent_id": None, "environment_id": 17}, {"user_id": 7})
    assert outcome.ok and calls["environment_id"] == 17 and calls["mode"] == "server"


@pytest.mark.asyncio
async def test_start_run_api_case_forces_server(monkeypatch):
    """接口用例 + 客户端选择 → 自动回退服务端；UI+接口混合 + 客户端 → 明确拒绝。"""
    conv = FakeConversation(project_id=30)

    class ApiCase:
        id = 501
        project_id = 30
        name = "接口用例"
        case_kind = "api"

    class UiCase:
        id = 502
        project_id = 30
        name = "UI 用例"
        case_kind = "ui"

    async def active(db, cid):
        return 0

    async def get_tc(db, cid):
        return {501: ApiCase(), 502: UiCase()}[cid]

    async def resolve_env(db, pid, eid=None):
        return Env(17, "默认环境", None, project_id=30), ""

    class User:
        id = 7

    async def load_user(db, uid):
        return User()

    calls = {}

    async def invoke(db, *, user, case_ids, mode, agent_name, environment_id):
        calls.update(mode=mode, agent_name=agent_name)
        return 456

    async def watch(cid, bid, **kw):
        return None

    monkeypatch.setattr(chat_run, "_active_run_count", active)
    monkeypatch.setattr(chat_run.crud, "get_test_case", get_tc)
    monkeypatch.setattr(chat_run, "_resolve_environment", resolve_env)
    monkeypatch.setattr(chat_run, "_load_user", load_user)
    monkeypatch.setattr(chat_run, "_invoke_batch", invoke)
    monkeypatch.setattr(chat_run, "_watch_run", watch)
    monkeypatch.setattr(chat_run.asyncio, "create_task", lambda coro: coro.close())

    card, err, _ = await chat_run.start_run(
        None, conversation=conv, case_ids=[501], mode="client", agent_id=26, user_id=7,
    )
    assert err == "" and calls["mode"] == "server" and card["agent_name"] is None

    card2, err2, _ = await chat_run.start_run(
        None, conversation=conv, case_ids=[501, 502], mode="client", agent_id=26, user_id=7,
    )
    assert card2 is None and "服务端" in err2


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
async def test_watch_run_tolerates_late_card_message(monkeypatch):
    """035 回归：卡片消息滞后落库（工具循环多轮）时，watcher 应等待而非早退。"""
    conv_id = 3
    batch_id = 505
    msg = FakeMessage(conversation_id=conv_id, role="assistant", cards=[
        {"type": "run", "batch_id": batch_id, "status": "running", "case_ids": [11],
         "progress": {"total": 1, "done": 0}},
    ])
    writes = []
    state = {"i": 0}

    class Batch:
        id = batch_id
        status = "passed"
        total_cases = 1
        passed = 1
        failed = 0

    async def fake_sleep(sec):
        state["i"] += 1

    async def get_batch(db, bid):
        return Batch()

    async def find_card(db, cid, bid):
        # 前 3 轮找不到（消息未落库），第 4 轮起可见
        if state["i"] < 4:
            return None, None
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

    await chat_run._watch_run(conv_id, batch_id, max_iterations=10)
    assert len(writes) == 1
    assert writes[0]["status"] == "done"
    assert writes[0]["result"] == {"passed": 1, "failed": 0}


@pytest.mark.asyncio
async def test_watch_run_gives_up_after_grace_when_card_never_appears(monkeypatch):
    conv_id = 3
    batch_id = 506
    state = {"i": 0, "finds": 0}

    class Batch:
        id = batch_id
        status = "running"
        total_cases = 1
        passed = 0
        failed = 0

    async def fake_sleep(sec):
        state["i"] += 1

    async def get_batch(db, bid):
        return Batch()

    async def find_card(db, cid, bid):
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
    monkeypatch.setattr(chat_run.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(chat_run.crud, "get_run_batch", get_batch)
    monkeypatch.setattr(chat_run, "_find_run_card_message", find_card)
    monkeypatch.setattr(chat_run, "emit", noop_emit)

    await chat_run._watch_run(conv_id, batch_id, max_iterations=100)
    assert state["finds"] == 60  # 宽限 60 轮后放弃（不无限等待）


@pytest.mark.asyncio
async def test_watch_run_survives_transient_db_error(monkeypatch):
    """单次轮询异常不应终止 watcher（应继续轮询直至终态）。"""
    conv_id = 3
    batch_id = 457
    msg = FakeMessage(conversation_id=conv_id, role="assistant", cards=[
        {"type": "run", "batch_id": batch_id, "status": "running", "case_ids": [11],
         "progress": {"total": 1, "done": 0}},
    ])
    writes = []

    class Batch:
        id = batch_id

        def __init__(self):
            self.status = "running"
            self.total_cases = 1
            self.passed = 0
            self.failed = 0

    batch = Batch()
    state = {"i": 0}

    async def fake_sleep(sec):
        state["i"] += 1
        if state["i"] == 3:
            batch.status = "failed"
            batch.failed = 1

    async def get_batch(db, bid):
        if state["i"] == 1:
            raise RuntimeError("db hiccup")
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
    assert writes, "首次异常后 watcher 应继续轮询"
    assert writes[-1]["status"] == "failed"


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


@pytest.mark.asyncio
async def test_start_run_rejects_functional_case(monkeypatch):
    """功能用例为手工用例：对话内执行明确拒绝并给出可操作提示。"""
    conv = FakeConversation(project_id=30)

    class FuncCase:
        id = 601
        project_id = 30
        name = "功能用例-购物车"

        case_kind = "functional"

    async def active(db, cid):
        return 0

    async def get_tc(db, cid):
        return FuncCase()

    monkeypatch.setattr(chat_run, "_active_run_count", active)
    monkeypatch.setattr(chat_run.crud, "get_test_case", get_tc)

    card, err, task_id = await chat_run.start_run(
        None, conversation=conv, case_ids=[601], mode="client", agent_id=26, user_id=7,
    )
    assert card is None and task_id is None
    assert "功能用例" in err and "手工" in err and "#601" in err
