# tests/unit/test_recordings.py
"""CDP 录制 API 端点单元测试。

覆盖 4 个端点：
  - POST /api/recordings/start
  - POST /api/recordings/{session_id}/stop
  - GET  /api/recordings/{session_id}/events
  - POST /api/recordings/{session_id}/convert

外部依赖（BrowserPool、CDPRecordingSession、convert_events_to_steps）全部 mock，
保证测试不发起真实网络请求或真实浏览器连接。
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from core.browser_pool import BrowserPool
from core.cdp_session import RecordedEvent
from app.routers.recordings import state as recording_state


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _inject_session(
    session_id: str,
    user_id: int,
    cdp_session_ref=None,
    status: str = "recording",
    url: str = "",
    page_title: str = "",
) -> recording_state.RecordingSessionState:
    """直接注入 session 到 in-memory state（绕过 async lock）。

    测试环境是单线程同步执行，无需真实的 asyncio.Lock 保护；
    这种注入方式比 asyncio.run(create_session(...)) 更简单、避免 loop 绑定问题。
    """
    state_obj = recording_state.RecordingSessionState(
        session_id=session_id,
        user_id=user_id,
        url=url,
        page_title=page_title,
        status=status,
        cdp_session_ref=cdp_session_ref,
    )
    recording_state._sessions[session_id] = state_obj
    recording_state._user_sessions[user_id] = session_id
    return state_obj


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clean_recording_state():
    """每个测试前后清空 in-memory state 与 BrowserPool，避免相互影响。"""
    recording_state._sessions.clear()
    recording_state._user_sessions.clear()
    BrowserPool._instances.clear()
    yield
    recording_state._sessions.clear()
    recording_state._user_sessions.clear()
    BrowserPool._instances.clear()


@pytest.fixture
def mock_manager():
    """构造一个 mock PlaywrightMCPManager。"""
    mgr = MagicMock()
    mgr.call_tool = AsyncMock(
        return_value={
            "success": True,
            "text": "ws://localhost:9222/devtools/browser/abc",
        }
    )
    mgr.session = MagicMock()
    return mgr


@pytest.fixture
def mock_cdp_session():
    """Mock 一个 CDPRecordingSession 实例。"""
    instance = MagicMock()
    instance.start_recording = AsyncMock(return_value=True)
    instance.stop_recording = AsyncMock(return_value=True)
    instance.get_events = MagicMock(return_value=[])  # 同步方法
    instance.collect_events = MagicMock(return_value=[])  # 同步方法
    instance.elapsed_seconds = 0.0
    instance.events_count = 0
    return instance


@pytest.fixture
def patched_cdp_session_cls(mock_cdp_session):
    """Patch `app.routers.recordings.CDPRecordingSession` 返回 mock 实例。"""
    with patch("app.routers.recordings.CDPRecordingSession") as MockCls:
        MockCls.return_value = mock_cdp_session
        yield MockCls, mock_cdp_session


@pytest.fixture
def admin_user(db, ensure_admin_user):
    """返回数据库中的 admin 用户对象（供 _inject_session 注入 user_id 使用）。"""
    import asyncio
    from sqlalchemy import select
    from app import db_models

    async def _get():
        result = await db.execute(
            select(db_models.User).where(db_models.User.username == "admin")
        )
        return result.scalar_one_or_none()

    return asyncio.run(_get())


# ===========================================================================
# POST /api/recordings/start
# ===========================================================================

class TestStartRecording:
    """POST /api/recordings/start 端点测试。"""

    @pytest.mark.asyncio
    async def test_start_no_browser_returns_503(self, client, admin_cookies):
        """BrowserPool 为空时启动录制应返回 503。"""
        # BrowserPool._instances 已被 _clean_recording_state 清空
        resp = client.post(
            "/api/recordings/start",
            json={"url": "https://example.com"},
            cookies=admin_cookies,
        )
        assert resp.status_code == 503
        body = resp.json()
        assert "无可用" in body["detail"] or "浏览器" in body["detail"]

    @pytest.mark.asyncio
    async def test_start_user_has_active_session_returns_409(
        self, client, admin_cookies, admin_user, mock_manager, patched_cdp_session_cls
    ):
        """用户已有 active session 时再次 start 应返回 409。"""
        # 先注入一个 active session（模拟已经存在一个未结束的录制）
        _inject_session(
            session_id="rec-existing",
            user_id=admin_user.id,
            cdp_session_ref=None,
            status="recording",
            url="https://other.com",
        )
        # 准备 BrowserPool
        BrowserPool._instances[1] = mock_manager

        resp = client.post(
            "/api/recordings/start",
            json={"url": "https://example.com"},
            cookies=admin_cookies,
        )
        assert resp.status_code == 409
        assert "已有进行中" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_start_cdp_failure_returns_500(
        self, client, admin_cookies, mock_manager, patched_cdp_session_cls
    ):
        """CDPRecordingSession.start_recording 返回 False 时应返回 500。"""
        MockCls, mock_inst = patched_cdp_session_cls
        # 让 start_recording 失败
        mock_inst.start_recording = AsyncMock(return_value=False)

        BrowserPool._instances[1] = mock_manager

        resp = client.post(
            "/api/recordings/start",
            json={"url": "https://example.com"},
            cookies=admin_cookies,
        )
        assert resp.status_code == 500
        assert "失败" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_start_success(
        self, client, admin_cookies, mock_manager, patched_cdp_session_cls
    ):
        """全部成功路径：返回 200，status=recording，session_id 以 'rec-' 开头。"""
        BrowserPool._instances[1] = mock_manager

        resp = client.post(
            "/api/recordings/start",
            json={"url": "https://example.com", "page_title": "Example"},
            cookies=admin_cookies,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "recording"
        assert data["session_id"].startswith("rec-")
        assert len(data["session_id"]) > 4
        assert data["url"] == "https://example.com"
        assert data["page_title"] == "Example"
        assert data["events_count"] == 0
        assert data["elapsed_seconds"] == 0.0

        # 验证 session 被注册到 state
        assert data["session_id"] in recording_state._sessions

    @pytest.mark.asyncio
    async def test_start_with_url_navigates(
        self, client, admin_cookies, mock_manager, patched_cdp_session_cls
    ):
        """提供 URL 时，应调用 call_tool('browser_navigate', {'url': ...})。"""
        BrowserPool._instances[1] = mock_manager

        resp = client.post(
            "/api/recordings/start",
            json={"url": "https://example.com"},
            cookies=admin_cookies,
        )
        assert resp.status_code == 200

        # 验证 browser_navigate 被调用（CDPRecordingSession 内部也调用过 call_tool，
        # 但 browser_navigate 只会从 start endpoint 触发）
        mock_manager.call_tool.assert_any_call(
            "browser_navigate", {"url": "https://example.com"}
        )

    @pytest.mark.asyncio
    async def test_start_requires_auth(self, client):
        """不带 cookies 应返回 401。"""
        resp = client.post(
            "/api/recordings/start",
            json={"url": "https://example.com"},
        )
        assert resp.status_code == 401


# ===========================================================================
# POST /api/recordings/{session_id}/stop
# ===========================================================================

class TestStopRecording:
    """POST /api/recordings/{session_id}/stop 端点测试。"""

    @pytest.mark.asyncio
    async def test_stop_nonexistent_returns_404(self, client, admin_cookies):
        """停止一个不存在的 session 应返回 404。"""
        resp = client.post(
            "/api/recordings/rec-notfound/stop",
            cookies=admin_cookies,
        )
        assert resp.status_code == 404
        assert "不存在" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_stop_not_recording_returns_400(
        self, client, admin_cookies, admin_user, mock_cdp_session
    ):
        """session 存在但 status != 'recording' 时 stop 应返回 400。"""
        # 注入一个 status='stopped' 的 session
        _inject_session(
            session_id="rec-already-stopped",
            user_id=admin_user.id,
            cdp_session_ref=mock_cdp_session,
            status="stopped",
        )

        resp = client.post(
            "/api/recordings/rec-already-stopped/stop",
            cookies=admin_cookies,
        )
        assert resp.status_code == 400
        detail = resp.json()["detail"]
        assert "状态" in detail or "recording" in detail

    @pytest.mark.asyncio
    async def test_stop_success(
        self, client, admin_cookies, admin_user, mock_cdp_session
    ):
        """正常停止：返回 200，status='stopped'，elapsed/events 由 cdp_session 提供。"""
        mock_cdp_session.elapsed_seconds = 12.5
        mock_cdp_session.events_count = 7

        _inject_session(
            session_id="rec-stop-ok",
            user_id=admin_user.id,
            cdp_session_ref=mock_cdp_session,
            status="recording",
            url="https://example.com",
            page_title="Stop Test",
        )

        resp = client.post(
            "/api/recordings/rec-stop-ok/stop",
            cookies=admin_cookies,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "stopped"
        assert data["session_id"] == "rec-stop-ok"
        assert data["url"] == "https://example.com"
        assert data["page_title"] == "Stop Test"
        assert data["elapsed_seconds"] == 12.5
        assert data["events_count"] == 7

        # state 中的 status 应被改为 'stopped'
        assert recording_state._sessions["rec-stop-ok"].status == "stopped"
        # cdp_session.stop_recording 被调用
        mock_cdp_session.stop_recording.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_stop_requires_auth(self, client):
        """不带 cookies 应返回 401。"""
        resp = client.post("/api/recordings/rec-anything/stop")
        assert resp.status_code == 401


# ===========================================================================
# GET /api/recordings/{session_id}/events
# ===========================================================================

class TestGetEvents:
    """GET /api/recordings/{session_id}/events 端点测试。"""

    @pytest.mark.asyncio
    async def test_get_events_nonexistent_returns_404(self, client, admin_cookies):
        """获取不存在 session 的 events 应返回 404。"""
        resp = client.get(
            "/api/recordings/rec-notfound/events",
            cookies=admin_cookies,
        )
        assert resp.status_code == 404
        assert "不存在" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_get_events_no_cdp_session_returns_empty(
        self, client, admin_cookies, admin_user
    ):
        """session 存在但 cdp_session_ref=None 时应返回 200 + []。"""
        _inject_session(
            session_id="rec-no-cdp",
            user_id=admin_user.id,
            cdp_session_ref=None,
            status="recording",
        )

        resp = client.get(
            "/api/recordings/rec-no-cdp/events",
            cookies=admin_cookies,
        )
        assert resp.status_code == 200
        assert resp.json() == []

    @pytest.mark.asyncio
    async def test_get_events_success(
        self, client, admin_cookies, admin_user, mock_cdp_session
    ):
        """session 有 events 时应返回事件列表。"""
        mock_cdp_session.get_events = MagicMock(
            return_value=[
                RecordedEvent(
                    event_type="click",
                    timestamp=1.0,
                    selector="#btn",
                    value=None,
                    url="https://x.com",
                    page_title="X",
                ),
                RecordedEvent(
                    event_type="input",
                    timestamp=2.0,
                    selector="#txt",
                    value="hello",
                    url="https://x.com",
                    page_title="X",
                ),
                RecordedEvent(
                    event_type="navigation",
                    timestamp=3.0,
                    value="https://x.com",
                    url="https://x.com",
                    page_title="X",
                ),
            ]
        )

        _inject_session(
            session_id="rec-events",
            user_id=admin_user.id,
            cdp_session_ref=mock_cdp_session,
        )

        resp = client.get(
            "/api/recordings/rec-events/events",
            cookies=admin_cookies,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) == 3

        assert data[0]["event_type"] == "click"
        assert data[0]["selector"] == "#btn"
        assert data[0]["timestamp"] == 1.0
        assert data[0]["url"] == "https://x.com"

        assert data[1]["event_type"] == "input"
        assert data[1]["value"] == "hello"

        assert data[2]["event_type"] == "navigation"

    @pytest.mark.asyncio
    async def test_get_events_does_not_clear_buffer(
        self, client, admin_cookies, admin_user, mock_cdp_session
    ):
        """get_events 不应清空 buffer：连续调用应返回相同内容。"""
        # 模拟 CDPRecordingSession.get_events 的非破坏性语义
        mock_cdp_session.get_events = MagicMock(
            return_value=[
                RecordedEvent(
                    event_type="click",
                    timestamp=1.0,
                    selector="#btn",
                    url="",
                ),
            ]
        )

        _inject_session(
            session_id="rec-buffer",
            user_id=admin_user.id,
            cdp_session_ref=mock_cdp_session,
        )

        resp1 = client.get(
            "/api/recordings/rec-buffer/events",
            cookies=admin_cookies,
        )
        resp2 = client.get(
            "/api/recordings/rec-buffer/events",
            cookies=admin_cookies,
        )

        assert resp1.status_code == 200
        assert resp2.status_code == 200
        # 两次响应内容相同
        assert resp1.json() == resp2.json()
        assert len(resp1.json()) == 1
        # get_events 被调用了两次
        assert mock_cdp_session.get_events.call_count == 2


# ===========================================================================
# POST /api/recordings/{session_id}/convert
# ===========================================================================

class TestConvert:
    """POST /api/recordings/{session_id}/convert 端点测试。"""

    @pytest.mark.asyncio
    async def test_convert_nonexistent_returns_404(self, client, admin_cookies):
        """转换不存在的 session 应返回 404。"""
        resp = client.post(
            "/api/recordings/rec-notfound/convert",
            json={"session_id": "rec-notfound"},
            cookies=admin_cookies,
        )
        assert resp.status_code == 404
        assert "不存在" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_convert_no_events_returns_empty_steps(
        self, client, admin_cookies, admin_user, mock_cdp_session
    ):
        """session 存在但无 events 时应返回 200 + steps=[]。"""
        mock_cdp_session.collect_events = MagicMock(return_value=[])

        _inject_session(
            session_id="rec-no-events",
            user_id=admin_user.id,
            cdp_session_ref=mock_cdp_session,
            page_title="",
        )

        resp = client.post(
            "/api/recordings/rec-no-events/convert",
            json={"session_id": "rec-no-events"},
            cookies=admin_cookies,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["session_id"] == "rec-no-events"
        assert data["steps"] == []
        assert data["events_count"] == 0

    @pytest.mark.asyncio
    async def test_convert_success(
        self, client, admin_cookies, admin_user, mock_cdp_session
    ):
        """session 有 events，convert_events_to_steps 被 mock，返回 200 + steps。"""
        mock_cdp_session.collect_events = MagicMock(
            return_value=[
                RecordedEvent(
                    event_type="click",
                    timestamp=1.0,
                    selector="#login",
                    url="https://x.com",
                    page_title="登录页",
                ),
                RecordedEvent(
                    event_type="input",
                    timestamp=2.0,
                    selector="#username",
                    value="admin",
                    url="https://x.com",
                    page_title="登录页",
                ),
            ]
        )

        _inject_session(
            session_id="rec-conv",
            user_id=admin_user.id,
            cdp_session_ref=mock_cdp_session,
            page_title="登录页",
        )

        # Mock LLM 转换函数
        mock_llm_result = [
            {
                "step_description": "点击登录按钮",
                "expected_result": "弹出登录表单",
            },
            {
                "step_description": "在用户名输入框中输入 admin",
                "expected_result": "输入框显示 admin",
            },
        ]
        with patch(
            "app.routers.recordings.convert_events_to_steps",
            new=AsyncMock(return_value=mock_llm_result),
        ):
            resp = client.post(
                "/api/recordings/rec-conv/convert",
                json={"session_id": "rec-conv"},
                cookies=admin_cookies,
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["session_id"] == "rec-conv"
        assert data["page_title"] == "登录页"
        assert data["events_count"] == 2
        assert len(data["steps"]) == 2

        assert data["steps"][0]["step_description"] == "点击登录按钮"
        assert data["steps"][0]["expected_result"] == "弹出登录表单"
        assert data["steps"][1]["step_description"] == "在用户名输入框中输入 admin"
        assert data["steps"][1]["expected_result"] == "输入框显示 admin"

    @pytest.mark.asyncio
    async def test_convert_requires_auth(self, client):
        """不带 cookies 应返回 401。"""
        resp = client.post(
            "/api/recordings/rec-x/convert",
            json={"session_id": "rec-x"},
        )
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_convert_session_id_mismatch(self, client, admin_cookies):
        """path 与 body 的 session_id 不一致应返回 400。"""
        resp = client.post(
            "/api/recordings/rec-path-id/convert",
            json={"session_id": "rec-body-id"},
            cookies=admin_cookies,
        )
        assert resp.status_code == 400
        detail = resp.json()["detail"]
        assert "不一致" in detail
