"""034 迭代（2026-10-10）：对话浏览器探索生成（browse_generate）。

覆盖：探索主循环（动作白名单 / ref 校验 / 破坏性护栏 / 同站 goto 守卫 /
凭据占位符替换与打码 / 步数上限 / done 总结）、报告材料组装、
客户端浏览器适配（observe/act/evaluate/screenshot/console）、服务处理器卡片（客户端/服务端标注）。
"""
import base64
import json
import os
from types import SimpleNamespace

import pytest

from core.chat import browse
from core.chat.browse import BrowseConfig, build_report_materials, run_exploration

# 1×1 透明 PNG（截图假实现写入用）
_PNG_1PX = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNgYGBgAAAABQAB"
    "h6FO1AAAAABJRU5ErkJggg=="
)


# ----------------------------
# 假浏览器 / 假候选
# ----------------------------

class FakeBrowser:
    mode = "server"
    label = "测试浏览器"

    def __init__(self, *, fail_actions=(), fail_snapshot=False):
        self.executed: list[dict] = []
        self.screenshots: list[str] = []
        self._fail_actions = set(fail_actions)
        self._fail_snapshot = fail_snapshot
        self._title = "首页"
        self._url = "http://site.local/"

    async def start(self): return None
    async def stop(self): return None

    async def snapshot(self):
        if self._fail_snapshot:
            raise RuntimeError("snapshot down")
        return "(snapshot)"

    async def evaluate_json(self, fn):
        return {"url": self._url, "title": self._title}

    async def page_facts(self):
        return {"url": self._url, "title": self._title}

    async def act(self, action):
        self.executed.append(dict(action))
        if action.get("action") == "goto":
            self._url = str(action.get("value") or self._url)
        if action.get("action") in self._fail_actions:
            return {"success": False, "error": f"{action['action']} failed"}
        return {"success": True}

    async def screenshot(self, path):
        self.screenshots.append(path)
        # 写一张真实小 PNG（视觉帧读取用）；1x1 透明像素即可
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "wb") as fh:
                fh.write(base64.b64decode(_PNG_1PX))
        except Exception:
            pass
        return path

    async def console(self, limit=20):
        return ["[ERROR] boom"]


def _candidate(ref, role="button", name="登录", text=""):
    return SimpleNamespace(ref=ref, role=role, name=name, text=text,
                           visible=True, enabled=True, obscured=False)


@pytest.fixture
def patch_candidates(monkeypatch):
    def _apply(cands):
        monkeypatch.setattr(
            "core.locator_candidates.extract_candidates", lambda snapshot, **kw: tuple(cands)
        )
        monkeypatch.setattr(
            "core.locator_candidates.actionable_candidates", lambda cs: tuple(cs)
        )
    return _apply


async def _llm_script(decisions):
    queue = list(decisions)

    async def _llm(messages):
        assert isinstance(messages, list) and messages[0]["role"] == "system"
        if not queue:
            raise AssertionError("llm called more than expected")
        item = queue.pop(0)
        return item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)

    return _llm


def _cfg(**kw):
    base = dict(url="http://site.local", goal="登录并查看主要功能", username="admin", password="Secret123")
    base.update(kw)
    return BrowseConfig(**base)


# ----------------------------
# 主循环
# ----------------------------

@pytest.mark.asyncio
async def test_timeline_note_event_emitted_before_step(patch_candidates):
    """ZCode 式时间线：决策 note 作为文本旁白事件先于该动作的 step 事件发出。"""
    patch_candidates([_candidate("e2")])
    browser = FakeBrowser()
    llm = await _llm_script([
        {"thinking": "点登录", "action": "click", "ref": "e2", "note": "准备点击登录按钮"},
        {"thinking": "完成", "action": "done", "summary": "OK", "features": []},
    ])
    events: list[dict] = []

    async def _on_event(evt):
        events.append(evt)

    report = await run_exploration(
        _cfg(), llm=llm, browser=browser, screenshot_dir="/tmp/opencode/bt_shots",
        on_event=_on_event,
    )
    assert report["ok"] is True
    notes = [e for e in events if e.get("kind") == "note"]
    assert [n["text"] for n in notes] == ["准备点击登录按钮"]
    # 顺序：note 先于随后的 step（goto step 之前/之后均可，但 note 必须早于对应 click 的 step）
    kinds = [e.get("kind") for e in events]
    note_at = kinds.index("note")
    click_step_at = next(
        i for i, e in enumerate(events)
        if e.get("kind") == "step" and "点击" in str(e.get("label") or "") or (
            e.get("kind") == "step" and "login" in str(e.get("label") or ""))
    ) if any(e.get("kind") == "step" for e in events) else None
    assert click_step_at is None or note_at < click_step_at

    # 凭据打码：note 中如含密码需打码（此处无密码，回归 redact 管线即可）
    assert all("Secret123" not in n["text"] for n in notes)


@pytest.mark.asyncio
async def test_happy_path_credentials_masked_and_substituted(patch_candidates):
    patch_candidates([_candidate("e2"), _candidate("e3"), _candidate("e4")])
    browser = FakeBrowser()
    llm = await _llm_script([
        {"thinking": "填账号", "action": "fill", "ref": "e2", "value": "<<USERNAME>>"},
        {"thinking": "填密码", "action": "fill", "ref": "e3", "value": "<<PASSWORD>>"},
        {"thinking": "提交", "action": "click", "ref": "e4", "note": "登录后进入首页"},
        {"thinking": "完成", "action": "done", "summary": "登录成功，主要功能：仪表盘/报表",
         "features": ["用户登录", "仪表盘", "报表导出"]},
    ])
    report = await run_exploration(_cfg(), llm=llm, browser=browser, screenshot_dir="/tmp/opencode/bt_shots")

    assert report["ok"] is True and report["stopped_reason"] == "done"
    assert report["summary"].startswith("登录成功")
    assert report["features"] == ["用户登录", "仪表盘", "报表导出"]
    # 凭据真实值已送达浏览器（占位符替换），但不落 journal
    fills = [a for a in browser.executed if a["action"] == "fill"]
    assert fills[0]["value"] == "admin" and fills[1]["value"] == "Secret123"
    j_fills = [s for s in report["steps"] if s["action"] == "fill"]
    assert j_fills[0]["value"] == "******" and j_fills[1]["value"] == "******"
    # 截图存证（起始页）
    assert browser.screenshots


@pytest.mark.asyncio
async def test_ref_validation_and_whitelist(patch_candidates):
    patch_candidates([_candidate("e1")])
    browser = FakeBrowser()
    llm = await _llm_script([
        {"thinking": "编造 ref", "action": "click", "ref": "e99"},
        {"thinking": "用禁术", "action": "evaluate", "value": "document.body.innerHTML=''"},
        {"thinking": "完成", "action": "done", "summary": "结束", "features": []},
    ])
    report = await run_exploration(_cfg(), llm=llm, browser=browser)
    errs = [s for s in report["steps"] if not s.get("ok")]
    assert any("ref 不在当前候选" in (s.get("error") or "") for s in errs)
    assert any("不在允许的动作内" in (s.get("error") or "") for s in errs)
    # 两个非法决策都没有真正执行（只有起始 goto）
    assert [a["action"] for a in browser.executed] == ["goto"]


@pytest.mark.asyncio
async def test_dangerous_click_guard(patch_candidates):
    patch_candidates([_candidate("e7", name="删除订单")])
    browser = FakeBrowser()
    llm = await _llm_script([
        {"thinking": "危险", "action": "click", "ref": "e7"},
        {"thinking": "完成", "action": "done", "summary": "跳过危险操作", "features": []},
    ])
    report = await run_exploration(_cfg(), llm=llm, browser=browser)
    assert any("禁止破坏性操作" in (s.get("error") or "") for s in report["steps"])
    assert all(a["action"] == "goto" for a in browser.executed)


@pytest.mark.asyncio
async def test_goto_same_site_guard(patch_candidates):
    patch_candidates([])
    browser = FakeBrowser()
    llm = await _llm_script([
        {"thinking": "跳外站", "action": "goto", "url": "http://evil.example/login"},
        {"thinking": "完成", "action": "done", "summary": "结束", "features": []},
    ])
    report = await run_exploration(_cfg(), llm=llm, browser=browser)
    assert any("只允许回到起始站点" in (s.get("error") or "") for s in report["steps"])
    assert all(a["action"] == "goto" for a in browser.executed)


@pytest.mark.asyncio
async def test_max_steps_partial_and_invalid_json(patch_candidates):
    patch_candidates([_candidate("e1")])
    browser = FakeBrowser()
    llm = await _llm_script([
        "不是 JSON",
        {"thinking": "点击", "action": "click", "ref": "e1"},
        {"thinking": "点击", "action": "click", "ref": "e1"},
    ])
    report = await run_exploration(_cfg(max_steps=3), llm=llm, browser=browser)
    assert report["stopped_reason"] == "max_steps"
    assert report["ok"] is True
    assert "达到步数上限" in report["summary"]
    assert any("未返回合法 JSON" in (s.get("error") or "") for s in report["steps"])


@pytest.mark.asyncio
async def test_start_page_failure_reports_honestly(patch_candidates):
    patch_candidates([])
    browser = FakeBrowser(fail_actions={"goto"})
    llm = await _llm_script([])
    report = await run_exploration(_cfg(), llm=llm, browser=browser)
    assert report["ok"] is False and report["stopped_reason"] == "error"
    assert "打开起始页失败" in (report.get("error") or "")


def test_build_report_materials():
    text = build_report_materials({
        "goal": "登录并查看主要功能", "started_url": "http://site.local", "summary": "main ok",
        "stopped_reason": "done", "browser_label": "客户端「本机」的浏览器",
        "pages": [
            {"url": "http://site.local/a", "title": "A 页"},
            {"url": "http://site.local/a", "title": "A 页"},  # 重复页面 → 去重
        ],
        "features": ["登录", "登录", "报表"],  # 重复功能点 → 去重
        "steps": [{"action": "click", "target": "button:登录", "ok": True, "note": "进入首页"}],
        "console": ["[ERROR] x"],
    })
    assert "# UI 自动化测试用例生成材料" in text
    assert "【生成要求" in text  # 防元信息污染：顶部生成要求
    assert "禁止把本材料的标题、章节名、元信息" in text
    assert "主要功能点" in text and "- 登录" in text
    assert text.count("- A 页") == 1  # 页面去重
    assert text.count("- 登录") == 1  # 功能点去重
    assert "A 页" in text and "客户端「本机」的浏览器" not in text  # 探索方式不再进材料
    # 二次修复：过程/控制台/网络等原始记录不再进材料（切断「打开【探索过程】」类污染源）
    assert "[ERROR] x" not in text
    assert "### 探索过程" not in text and "### 网络请求异常" not in text
    assert "button:登录" not in text


def test_build_report_materials_features_empty_honest_note():
    text = build_report_materials({
        "started_url": "http://site.local", "summary": "未及总结",
        "pages": [{"url": "http://site.local/a", "title": "A 页"}],
        "features": [],
    })
    assert "未及总结主要功能点" in text and "宁少勿滥" in text


def test_build_report_materials_feature_count_directive():
    text = build_report_materials({
        "started_url": "http://site.local", "summary": "ok",
        "pages": [{"url": "http://site.local/a", "title": "A 页"}],
        "features": ["功能一", "功能二", "功能三"],
    })
    assert "主要功能点共 3 条" in text and "每条功能点生成 1 条" in text


# ----------------------------
# 客户端浏览器适配
# ----------------------------

class FakeAgentManager:
    def __init__(self, *, observe=None, act=None, eval_text=None, boot_ok=True):
        self.sent_observe = []
        self.sent_act = []
        self.get_snapshot_calls = 0
        self._observe = observe or {"success": True, "snapshot": "(snap)",
                                   "page_url": "http://site.local/", "page_title": "首页"}
        self._act = act or {"success": True}
        self._eval_text = eval_text
        self._boot_ok = boot_ok

    async def send_get_snapshot(self, agent, run_id):
        self.get_snapshot_calls += 1
        if not self._boot_ok:
            return {"success": False, "error": "boot failed"}
        return {"text": "(booted)"}

    async def send_observe(self, agent, run_id, want_screenshot=False, **kw):
        self.sent_observe.append((agent, run_id, want_screenshot))
        if want_screenshot and self._observe.get("screenshot_b64") is None:
            return {**self._observe, "screenshot_b64": base64.b64encode(b"PNGDATA").decode()}
        return dict(self._observe)

    async def send_act(self, agent, run_id, tool_call):
        self.sent_act.append((agent, run_id, dict(tool_call)))
        if tool_call.get("name") == "evaluate":
            return {"success": True, "text": self._eval_text or '"{\\"url\\":\\"u\\", \\"title\\":\\"t\\"}"'}
        if tool_call.get("name") == "console":
            return {"success": True, "text": "[ERROR] x\nReturning 0 messages for level \"error\""}
        return dict(self._act)


@pytest.mark.asyncio
async def test_client_browser_adapter(tmp_path):
    mgr = FakeAgentManager()
    cb = browse.ClientBrowser(mgr, "本机客户端", "browse_1")
    cb.label = "客户端「本机客户端」的浏览器"

    assert await cb.snapshot() == "(snap)"
    # URL/标题取 observe 回传（客户端 evaluate 成功路径不回传 text）
    assert await cb.page_facts() == {"url": "http://site.local/", "title": "首页"}
    facts = await cb.evaluate_json("() => JSON.stringify({url:1})")
    assert facts == {"url": "u", "title": "t"}
    res = await cb.act({"action": "click", "selector": "e1"})
    assert res["success"] is True
    assert mgr.sent_act[-1][2]["action"] == "click"

    shot = tmp_path / "s.png"
    saved = await cb.screenshot(str(shot))
    assert saved == str(shot) and shot.read_bytes() == b"PNGDATA"

    entries = await cb.console()
    assert entries == ["[ERROR] x"]  # 零命中计数行被跳过


@pytest.mark.asyncio
async def test_client_browser_snapshot_failure():
    mgr = FakeAgentManager(observe={"success": False, "error": "offline"})
    cb = browse.ClientBrowser(mgr, "本机客户端", "browse_1")
    with pytest.raises(RuntimeError):
        await cb.snapshot()


@pytest.mark.asyncio
async def test_client_browser_start_boots_browser():
    """冷启动：start() 必须经 get_snapshot 拉起客户端浏览器（observe/act 不会拉起）。"""
    mgr = FakeAgentManager()
    cb = browse.ClientBrowser(mgr, "本机客户端", "browse_1")
    await cb.start()
    assert mgr.get_snapshot_calls == 1

    bad = FakeAgentManager(boot_ok=False)
    cb2 = browse.ClientBrowser(bad, "本机客户端", "browse_2")
    with pytest.raises(RuntimeError):
        await cb2.start()


# ----------------------------
# 服务处理器：卡片标注浏览器去向
# ----------------------------

class FakeDB:
    def __init__(self, agent_name="本机客户端-验证"):
        self._agent_name = agent_name

    async def get(self, model, oid):
        return SimpleNamespace(id=oid, name=self._agent_name)


@pytest.mark.asyncio
async def test_handler_marks_client_browser(monkeypatch):
    from app.services import chat_browse

    spawned = []

    async def _fake_task(**kw):
        spawned.append(kw)

    monkeypatch.setattr(chat_browse, "_run_browse_task", _fake_task)

    conv = SimpleNamespace(
        id=19, project_id=30, agent_id=26, assistant_agent_id=None,
    )
    outcome = await chat_browse.browse_generate_handler(
        FakeDB(), conv,
        {"type": "browse_generate", "url": "http://127.0.0.1:8002", "goal": "登录并看主要功能",
         "username": "admin", "password": "x", "kind": "ui"},
        {"user_id": 7},
    )
    assert outcome.card["browser_mode"] == "client"
    assert outcome.card["agent_name"] == "本机客户端-验证"
    assert "客户端" in outcome.note
    assert outcome.card["status"] == "running"
    # 凭据不落卡片
    assert "x" not in json.dumps(outcome.card, ensure_ascii=False)


@pytest.mark.asyncio
async def test_handler_server_browser_when_no_client(monkeypatch):
    from app.services import chat_browse

    async def _fake_task(**kw):
        return None

    monkeypatch.setattr(chat_browse, "_run_browse_task", _fake_task)

    conv = SimpleNamespace(id=20, project_id=30, agent_id=0, assistant_agent_id=None)
    outcome = await chat_browse.browse_generate_handler(
        FakeDB(), conv,
        {"type": "browse_generate", "url": "http://site.local", "goal": "探索", "kind": "ui"},
        {"user_id": 7},
    )
    assert outcome.card["browser_mode"] == "server"
    assert "服务端" in outcome.note


# ----------------------------
# 凭据防泄漏：目标/回复/思考打码
# ----------------------------

def test_validate_redacts_goal_secrets():
    from core.chat.contracts import validate_action

    norm, err = validate_action({
        "type": "browse_generate", "url": "127.0.0.1:8002",
        "goal": "登录 127.0.0.1:8002，用户名 admin，密码 Secret123，看主要功能",
        "username": "admin", "password": "Secret123",
    })
    assert err == ""
    assert "Secret123" not in norm["goal"] and "admin" not in norm["goal"]
    assert norm["password"] == "Secret123"  # 动作保留明文（执行侧需要）
    assert norm["url"] == "http://127.0.0.1:8002"


def test_redact_decision_secrets_reply_and_thinking():
    from core.chat.contracts import redact_decision_secrets

    d = redact_decision_secrets({
        "thinking": "用户给了密码 Secret123，先登录",
        "reply": "我会用 admin/Secret123 登录",
        "actions": [{
            "type": "browse_generate", "url": "http://s", "goal": "用 admin 和 Secret123 登录",
            "username": "admin", "password": "Secret123",
        }],
    })
    assert "Secret123" not in d["reply"] and "Secret123" not in d["thinking"]
    assert "Secret123" not in d["actions"][0]["goal"]
    assert d["actions"][0]["password"] == "Secret123"

    # 无凭据动作 → 原样返回
    d2 = redact_decision_secrets({"reply": "普通回复", "actions": []})
    assert d2["reply"] == "普通回复"


# ----------------------------
# 视觉：每轮决策附带当前页面截图（2026-10-14）
# ----------------------------

def test_frame_to_data_url(tmp_path):
    path = os.path.join(tmp_path, "s.png")
    with open(path, "wb") as fh:
        fh.write(base64.b64decode(_PNG_1PX))
    url = browse.frame_to_data_url(path)
    assert url and url.startswith("data:image/jpeg;base64,")
    assert browse.frame_to_data_url(os.path.join(tmp_path, "missing.png")) is None


@pytest.mark.asyncio
async def test_vision_frame_attached_each_round(patch_candidates, tmp_path):
    patch_candidates([_candidate("e2")])
    browser = FakeBrowser()
    contents: list = []

    async def llm(messages):
        contents.append(messages[1]["content"])
        return json.dumps({"thinking": "完成", "action": "done", "summary": "ok"}, ensure_ascii=False)

    report = await run_exploration(_cfg(), llm=llm, browser=browser, screenshot_dir=str(tmp_path))
    assert report["ok"] is True and contents
    assert isinstance(contents[0], list) and len(contents[0]) >= 2
    assert contents[0][0]["type"] == "text" and contents[0][1]["type"] == "image_url"
    assert str(contents[0][1]["image_url"]["url"]).startswith("data:image/jpeg;base64,")


@pytest.mark.asyncio
async def test_vision_disabled_text_only(patch_candidates, tmp_path):
    patch_candidates([_candidate("e2")])
    browser = FakeBrowser()
    seen: list = []

    async def llm(messages):
        seen.append(messages[1]["content"])
        return json.dumps({"thinking": "完成", "action": "done", "summary": "ok"}, ensure_ascii=False)

    await run_exploration(_cfg(vision=False), llm=llm, browser=browser, screenshot_dir=str(tmp_path))
    assert isinstance(seen[0], str) and "候选" in seen[0]


@pytest.mark.asyncio
async def test_vision_image_error_falls_back_text_only(patch_candidates, tmp_path):
    """换到不支持图片的模型时：首轮带图失败 → 纯文本降级重试一次，其余轮次关闭视觉。"""
    patch_candidates([_candidate("e2")])
    browser = FakeBrowser()
    contents: list = []

    async def llm(messages):
        content = messages[1]["content"]
        contents.append(content)
        if isinstance(content, list):
            raise RuntimeError("image_url unsupported by this model")
        return json.dumps({"thinking": "完成", "action": "done", "summary": "ok"}, ensure_ascii=False)

    report = await run_exploration(_cfg(), llm=llm, browser=browser, screenshot_dir=str(tmp_path))
    assert report["ok"] is True and report["stopped_reason"] == "done"
    assert isinstance(contents[0], list) and isinstance(contents[1], str)


# ----------------------------
# 候选渲染截断保护（2026-10-19：函件录入类页面「新增」按钮不可见问题）
# ----------------------------

def test_render_candidates_labels_nonstandard_buttons_and_notice():
    """div 按钮带标签；候选超限时保控件 + 显式告知截断（不许静默）。"""
    from core.locator_candidates import LocatorCandidate

    toolbar = (
        LocatorCandidate("f1e9", "generic", "新增", "新增", {"cursor": "pointer"}, ()),
        LocatorCandidate("f1e10", "generic", "删除", "删除", {"cursor": "pointer"}, ()),
    )
    links = tuple(
        LocatorCandidate(f"f1e{100 + i}", "link", f"行{i}", f"行{i}", {}, ())
        for i in range(1, 120)
    )
    text = browse._render_candidates(toolbar + links)

    assert '"新增"' in text and '"删除"' in text  # 非标准控件标签可见
    assert "候选共 121 个" in text and "此处显示 80 个" in text  # 截断告知


def test_render_candidates_no_notice_when_fits():
    from core.locator_candidates import LocatorCandidate

    small = (
        LocatorCandidate("e1", "button", "登录", "登录", {}, ()),
        LocatorCandidate("e2", "textbox", "", "", {}, ()),
    )
    text = browse._render_candidates(small)
    assert "候选共" not in text
    assert "[1] e1 | button | \"登录\"" in text


class _BrowserWithSpanText(FakeBrowser):
    """快照里「新增」是 span 文本（没有自己的 ref，只有承载容器 e2）。"""

    async def snapshot(self):
        return (
            "- generic [ref=e2]:\n"
            "  - text: 新增\n"
            "  - link \"删除\" [ref=e3] [cursor=pointer]\n"
        )


@pytest.mark.asyncio
async def test_click_text_fallback_for_span_button(patch_candidates):
    """span 包文本的按钮：候选里只有承载容器 → click + text 文案兜底成功执行。"""
    patch_candidates([_candidate("e2", role="generic", name="")])
    browser = _BrowserWithSpanText()
    llm = await _llm_script([
        {"thinking": "文案点击", "action": "click", "text": "新增"},
        {"thinking": "完成", "action": "done", "summary": "点击完成", "features": []},
    ])
    report = await run_exploration(_cfg(), llm=llm, browser=browser)
    clicks = [a for a in browser.executed if a["action"] == "click"]
    assert clicks and clicks[0]["selector"] == "text=新增"
    step = next(s for s in report["steps"] if s["action"] == "click")
    assert step["ok"] and step["target"] == "text:新增"


@pytest.mark.asyncio
async def test_click_text_fallback_rejects_text_not_in_snapshot(patch_candidates):
    """文案必须与当前快照逐字一致——不在快照中的 text 被拒绝（防编造）。"""
    patch_candidates([_candidate("e2", role="generic", name="")])
    browser = _BrowserWithSpanText()
    llm = await _llm_script([
        {"thinking": "编造文案", "action": "click", "text": "删除全部数据"},
        {"thinking": "完成", "action": "done", "summary": "结束", "features": []},
    ])
    report = await run_exploration(_cfg(), llm=llm, browser=browser)
    assert any("text 不在当前页面快照中" in (s.get("error") or "") for s in report["steps"])
    assert all(a["action"] == "goto" for a in browser.executed)
