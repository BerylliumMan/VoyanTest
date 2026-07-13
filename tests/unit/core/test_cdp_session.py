# tests/unit/core/test_cdp_session.py
"""Direct unit tests for ``core/cdp_session.py``.

The existing ``tests/unit/test_recordings.py`` covers the HTTP API layer and
mocks ``CDPRecordingSession`` entirely. This file exercises the real
``RecordedEvent`` dataclass, the ``VALID_EVENT_TYPES`` constant, and the
``CDPRecordingSession`` orchestrator's pure logic — without mocking the unit
under test.

CDP-connection paths (``_open_websocket``, ``_send_cdp`` over a real
WebSocket, ``_read_ws_loop``, etc.) require a live browser/CDP endpoint and
are deliberately not exercised here. Instead, the synchronous ingestion
methods (``record_event``, ``collect_events``, ``get_events``) and the
private event handlers are covered in depth. The async lifecycle methods
(``start_recording`` / ``stop_recording``) are exercised only on their
non-CDP branches (already-recording, not-recording, unsupported target).
"""
from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.cdp_session import (
    VALID_EVENT_TYPES,
    CDPRecordingSession,
    RecordedEvent,
)


# ===========================================================================
# TestRecordedEvent
# ===========================================================================


class TestRecordedEvent:
    """Tests for the ``RecordedEvent`` dataclass."""

    def test_create_minimal(self):
        """只传 event_type / timestamp，其余字段使用默认。"""
        ev = RecordedEvent(event_type="click", timestamp=1.23)
        assert ev.event_type == "click"
        assert ev.timestamp == 1.23
        # Defaults
        assert ev.selector is None
        assert ev.value is None
        assert ev.url == ""
        assert ev.screenshot is None
        assert ev.page_title == ""

    def test_create_full(self):
        """所有字段显式赋值。"""
        ev = RecordedEvent(
            event_type="input",
            timestamp=2.5,
            selector="#username",
            value="admin",
            url="https://example.com/login",
            screenshot="base64data",
            page_title="Login",
        )
        assert ev.event_type == "input"
        assert ev.timestamp == 2.5
        assert ev.selector == "#username"
        assert ev.value == "admin"
        assert ev.url == "https://example.com/login"
        assert ev.screenshot == "base64data"
        assert ev.page_title == "Login"

    def test_to_dict(self):
        """to_dict() 返回与字段一一对应的 dict。"""
        ev = RecordedEvent(
            event_type="navigation",
            timestamp=3.14,
            selector=None,
            value="https://example.com",
            url="https://example.com",
            page_title="Home",
        )
        d = ev.to_dict()
        assert d == {
            "event_type": "navigation",
            "timestamp": 3.14,
            "selector": None,
            "value": "https://example.com",
            "url": "https://example.com",
            "screenshot": None,
            "page_title": "Home",
        }

    def test_is_valid_valid(self):
        """所有 VALID_EVENT_TYPES 内的类型 is_valid() → True。"""
        for et in VALID_EVENT_TYPES:
            ev = RecordedEvent(event_type=et, timestamp=0.0)
            assert ev.is_valid() is True, f"{et} 应被识别为合法类型"

    def test_is_valid_invalid(self):
        """不在白名单内的类型 → False。"""
        for bad in ("", "scroll", "hover", "keypress", "Mouse.click", "NAVIGATION"):
            ev = RecordedEvent(event_type=bad, timestamp=0.0)
            assert ev.is_valid() is False, f"{bad!r} 应被识别为非法类型"

    def test_to_dict_roundtrip(self):
        """to_dict() 输出可通过 RecordedEvent(**d) 重建并与原对象相等。"""
        original = RecordedEvent(
            event_type="select",
            timestamp=9.81,
            selector="#country",
            value="CN",
            url="https://example.com",
            screenshot=None,
            page_title="Form",
        )
        d = original.to_dict()
        rebuilt = RecordedEvent(**d)
        assert rebuilt == original
        # 显式断言每个字段，防止 dataclass __eq__ 行为变化
        assert rebuilt.event_type == original.event_type
        assert rebuilt.timestamp == original.timestamp
        assert rebuilt.selector == original.selector
        assert rebuilt.value == original.value
        assert rebuilt.url == original.url
        assert rebuilt.screenshot == original.screenshot
        assert rebuilt.page_title == original.page_title


# ===========================================================================
# TestVALIDEventTypes
# ===========================================================================


class TestVALIDEventTypes:
    """VALID_EVENT_TYPES 常量测试。"""

    def test_valide_event_types_frozenset(self):
        """VALID_EVENT_TYPES 是 frozenset，元素不可变。"""
        assert isinstance(VALID_EVENT_TYPES, frozenset)

    def test_contains_expected_types(self):
        """包含全部 7 个预期事件类型。"""
        expected = {
            "navigation",
            "click",
            "input",
            "select",
            "screenshot",
            "wait",
            "assertion",
        }
        assert expected.issubset(VALID_EVENT_TYPES)
        # 确认没有意外的类型被添加
        assert VALID_EVENT_TYPES == expected

    def test_types_are_lowercase_strings(self):
        """所有元素都是小写字符串。"""
        for et in VALID_EVENT_TYPES:
            assert isinstance(et, str)
            assert et == et.lower(), f"{et!r} 应为小写"


# ===========================================================================
# TestCDPRecordingSessionInit
# ===========================================================================


class TestCDPRecordingSessionInit:
    """``CDPRecordingSession.__init__`` 与只读属性测试。"""

    def test_init(self):
        """__init__ 后：session_id 正确，事件列表为空，is_recording=False。"""
        s = CDPRecordingSession("rec-abc")
        assert s.session_id == "rec-abc"
        assert s.events_count == 0
        assert s.is_recording is False
        # 内部 events 列表初始为空
        assert s._events == []
        assert s._recording is False
        assert s._start_time is None
        # CDP 资源在初始化时未分配
        assert s._cdp_session is None
        assert s._cdp_url is None
        assert s._ws is None
        assert s._reader_task is None

    def test_session_id_property(self):
        """session_id 属性返回构造函数传入的值。"""
        assert CDPRecordingSession("rec-1").session_id == "rec-1"
        assert CDPRecordingSession("rec-xyz-123").session_id == "rec-xyz-123"
        # 空字符串也是合法的 session_id
        assert CDPRecordingSession("").session_id == ""

    def test_is_recording_default(self):
        """初始 is_recording 为 False。"""
        s = CDPRecordingSession("rec-default")
        assert s.is_recording is False

    def test_elapsed_seconds_before_start(self):
        """未启动时 elapsed_seconds 返回 0.0。"""
        s = CDPRecordingSession("rec-not-started")
        assert s.elapsed_seconds == 0.0

    def test_elapsed_seconds_after_start_time_set(self):
        """_start_time 设置后 elapsed_seconds ≥ 0。"""
        s = CDPRecordingSession("rec-running")
        s._start_time = time.time() - 2.5
        assert s.elapsed_seconds >= 2.0  # 至少 2 秒
        assert s.elapsed_seconds < 5.0  # 不应远大于设定值

    def test_elapsed_seconds_future_start_clamped_to_zero(self):
        """_start_time 在未来时（时钟漂移）被钳制到 0.0 而非负数。"""
        s = CDPRecordingSession("rec-future")
        s._start_time = time.time() + 100.0
        assert s.elapsed_seconds == 0.0

    def test_events_count_init(self):
        """events_count 初始为 0。"""
        assert CDPRecordingSession("rec-x").events_count == 0


# ===========================================================================
# TestRecordEvent
# ===========================================================================


class TestRecordEvent:
    """``record_event`` 同步事件注入测试。"""

    def test_record_event(self):
        """有效 payload → 追加一个事件，events_count 递增。"""
        s = CDPRecordingSession("rec-r1")
        s.record_event({"event_type": "click", "selector": "#btn", "url": "https://x"})
        assert s.events_count == 1
        ev = s._events[0]
        assert ev.event_type == "click"
        assert ev.selector == "#btn"
        assert ev.url == "https://x"

    def test_record_event_appends_multiple(self):
        """连续追加多个事件，events_count 等于追加次数。"""
        s = CDPRecordingSession("rec-r2")
        for i in range(5):
            s.record_event({"event_type": "click", "value": str(i)})
        assert s.events_count == 5
        assert [e.value for e in s._events] == ["0", "1", "2", "3", "4"]

    def test_record_event_invalid_type(self):
        """非法 event_type 被忽略（不抛异常、不修改 events）。"""
        s = CDPRecordingSession("rec-bad")
        s.record_event({"event_type": "scroll"})
        s.record_event({"event_type": ""})  # 空字符串也非法
        s.record_event({"event_type": "Mouse.click"})  # 大小写敏感
        assert s.events_count == 0
        assert s._events == []

    def test_record_event_non_dict(self):
        """非 dict 类型的 payload 被忽略。"""
        s = CDPRecordingSession("rec-non-dict")
        for bad in (None, "click", 42, ["click"], ("click",), object()):
            s.record_event(bad)
        assert s.events_count == 0

    def test_record_event_missing_event_type(self):
        """缺少 event_type 键等同于传入非法类型 → 忽略。"""
        s = CDPRecordingSession("rec-missing-type")
        s.record_event({"selector": "#x"})
        s.record_event({})
        assert s.events_count == 0

    def test_record_event_sets_defaults(self):
        """可选字段缺失时使用默认值（url='', page_title='', selector=None, value=None）。"""
        s = CDPRecordingSession("rec-defaults")
        s.record_event({"event_type": "click"})
        ev = s._events[0]
        assert ev.event_type == "click"
        assert ev.selector is None
        assert ev.value is None
        assert ev.url == ""
        assert ev.page_title == ""
        assert ev.screenshot is None
        # timestamp 是 float，且接近当前时间
        assert isinstance(ev.timestamp, float)
        assert ev.timestamp > 0

    def test_record_event_uses_last_page_url(self):
        """未传 url / page_title 时回退到 _last_page_url / _last_page_title。"""
        s = CDPRecordingSession("rec-anchor")
        s._last_page_url = "https://cached.com/page"
        s._last_page_title = "Cached Title"
        s.record_event({"event_type": "click", "selector": "#x"})
        ev = s._events[0]
        assert ev.url == "https://cached.com/page"
        assert ev.page_title == "Cached Title"

    def test_record_event_explicit_url_overrides_last(self):
        """显式传入 url 时优先生效，不被 _last_page_url 覆盖。"""
        s = CDPRecordingSession("rec-override")
        s._last_page_url = "https://stale.com"
        s.record_event({"event_type": "click", "url": "https://fresh.com"})
        assert s._events[0].url == "https://fresh.com"

    def test_record_event_explicit_timestamp(self):
        """显式 timestamp 优先于 time.time()。"""
        s = CDPRecordingSession("rec-ts")
        s.record_event({"event_type": "click", "timestamp": 100.5})
        assert s._events[0].timestamp == 100.5

    def test_record_event_timestamp_falls_back_to_now(self):
        """未传 timestamp 时使用 time.time()（接近当前时间）。"""
        before = time.time()
        s = CDPRecordingSession("rec-ts-now")
        s.record_event({"event_type": "click"})
        after = time.time()
        ts = s._events[0].timestamp
        assert before <= ts <= after

    def test_record_event_extra_keys_ignored(self):
        """多余的键被忽略，不影响已设置字段。"""
        s = CDPRecordingSession("rec-extra")
        s.record_event(
            {
                "event_type": "input",
                "selector": "#q",
                "value": "x",
                "url": "u",
                "extra_unknown_key": "ignored",
                "another": 123,
            }
        )
        ev = s._events[0]
        assert ev.event_type == "input"
        assert ev.selector == "#q"
        assert ev.value == "x"
        assert ev.url == "u"

    def test_record_event_url_coerced_to_string(self):
        """非字符串 url 被强制转 str。"""
        s = CDPRecordingSession("rec-coerce")
        s.record_event({"event_type": "click", "url": 12345})
        assert s._events[0].url == "12345"
        assert isinstance(s._events[0].url, str)


# ===========================================================================
# TestEventsBuffer
# ===========================================================================


class TestEventsBuffer:
    """``collect_events`` / ``get_events`` 行为对比。"""

    def test_collect_events(self):
        """collect_events 返回事件并清空 buffer。"""
        s = CDPRecordingSession("rec-collect")
        s.record_event({"event_type": "click"})
        s.record_event({"event_type": "input"})

        events = s.collect_events()
        assert len(events) == 2
        assert events[0].event_type == "click"
        assert events[1].event_type == "input"
        # buffer 已被清空
        assert s.events_count == 0
        assert s._events == []

    def test_collect_events_empty(self):
        """无事件时 collect_events 返回空列表。"""
        s = CDPRecordingSession("rec-empty")
        assert s.collect_events() == []
        # 重复调用仍安全
        assert s.collect_events() == []

    def test_collect_events_returns_copy(self):
        """collect_events 返回的是拷贝，外部修改不影响 session 状态。"""
        s = CDPRecordingSession("rec-copy")
        s.record_event({"event_type": "click"})
        events = s.collect_events()
        events.clear()  # 外部修改返回值
        # 二次 collect_events 仍为 0（buffer 已被前面那次清空）
        assert s.collect_events() == []

    def test_get_events_does_not_clear(self):
        """get_events 多次调用返回相同结果，buffer 不被清空。"""
        s = CDPRecordingSession("rec-get")
        s.record_event({"event_type": "click"})
        s.record_event({"event_type": "input"})

        first = s.get_events()
        second = s.get_events()
        assert len(first) == 2
        assert len(second) == 2
        # 两次返回内容相同
        assert [e.event_type for e in first] == [e.event_type for e in second]
        # buffer 未被清空
        assert s.events_count == 2

    def test_get_events_empty(self):
        """无事件时 get_events 返回空列表。"""
        s = CDPRecordingSession("rec-get-empty")
        assert s.get_events() == []

    def test_get_events_returns_copy(self):
        """get_events 返回的是拷贝，外部 append 不影响 session。"""
        s = CDPRecordingSession("rec-get-copy")
        s.record_event({"event_type": "click"})
        events = s.get_events()
        events.append(RecordedEvent(event_type="fake", timestamp=0.0))
        # session 内部 buffer 不变
        assert s.events_count == 1

    def test_get_events_vs_collect_events(self):
        """get_events 保留 buffer，collect_events 清空 buffer。"""
        s = CDPRecordingSession("rec-vs")
        s.record_event({"event_type": "click"})

        # 第一次 get_events：buffer 保留
        s.get_events()
        assert s.events_count == 1

        # 第一次 collect_events：buffer 清空
        s.collect_events()
        assert s.events_count == 0

        # 此后 get_events 应返回空
        assert s.get_events() == []


# ===========================================================================
# TestLifecycleGuards
# ===========================================================================


class TestLifecycleGuards:
    """``start_recording`` / ``stop_recording`` 中不依赖 CDP 真实连接的纯逻辑分支。"""

    @pytest.mark.asyncio
    async def test_start_recording_already_recording(self):
        """已经 recording 时再调用 start_recording 返回 False 且不抛异常。"""
        s = CDPRecordingSession("rec-already")
        s._recording = True  # 模拟已启动状态
        result = await s.start_recording("ws://127.0.0.1:9222")
        assert result is False

    @pytest.mark.asyncio
    async def test_start_recording_target_none(self):
        """target=None 时 _attach_cdp 返回 False → start_recording 返回 False。"""
        s = CDPRecordingSession("rec-none")
        result = await s.start_recording(None)
        assert result is False
        assert s.is_recording is False
        # 失败路径不应设置 start_time
        assert s._start_time is None

    @pytest.mark.asyncio
    async def test_start_recording_unsupported_target(self):
        """既无 call_tool 也不是 str 也不是 Playwright Page 的 target → False。"""
        s = CDPRecordingSession("rec-unsupported")
        result = await s.start_recording(42)  # int 不被任何分支支持
        assert result is False
        assert s.is_recording is False

    @pytest.mark.asyncio
    async def test_start_recording_clean_state_on_failure(self):
        """启动失败后，_recording/_start_time 保持初始状态（_safe_detach 已调用）。"""
        s = CDPRecordingSession("rec-cleanup")
        # 用 None 触发 _attach_cdp 失败 → 走 except 路径 → 调用 _safe_detach
        await s.start_recording(None)
        # 状态应保持初始
        assert s._recording is False
        assert s._start_time is None
        assert s._cdp_session is None
        assert s._ws is None
        assert s._reader_task is None

    @pytest.mark.asyncio
    async def test_stop_recording_not_recording(self):
        """未 recording 时 stop_recording 是 no-op，返回 True。"""
        s = CDPRecordingSession("rec-stop-idle")
        result = await s.stop_recording()
        assert result is True
        # 状态未变
        assert s.is_recording is False

    @pytest.mark.asyncio
    async def test_stop_recording_idempotent(self):
        """多次 stop_recording 调用均返回 True。"""
        s = CDPRecordingSession("rec-stop-multi")
        for _ in range(3):
            assert await s.stop_recording() is True


# ===========================================================================
# TestCDPEventHandlers
# ===========================================================================


class TestCDPEventHandlers:
    """内部事件处理回调（不依赖真实 CDP 传输）。"""

    def test_on_page_frame_navigated_records_navigation(self):
        """_on_page_frame_navigated 写入 navigation 事件并更新 _last_page_url。"""
        s = CDPRecordingSession("rec-nav")
        s._on_page_frame_navigated(
            {"frame": {"url": "https://example.com/path"}}
        )
        assert s.events_count == 1
        ev = s._events[0]
        assert ev.event_type == "navigation"
        assert ev.value == "https://example.com/path"
        assert ev.url == "https://example.com/path"
        # _last_page_url 已被更新
        assert s._last_page_url == "https://example.com/path"

    def test_on_page_frame_navigated_missing_frame(self):
        """params 缺 'frame' 字段时使用 _last_page_url 作为兜底。"""
        s = CDPRecordingSession("rec-nav-fallback")
        s._last_page_url = "https://previous.com"
        s._on_page_frame_navigated({})
        ev = s._events[0]
        assert ev.event_type == "navigation"
        assert ev.url == "https://previous.com"
        assert ev.value == "https://previous.com"

    def test_on_page_frame_navigated_malformed(self):
        """params['frame'] 不是 dict 时走 except 路径，事件仍被记录。"""
        s = CDPRecordingSession("rec-nav-bad")
        s._last_page_url = "https://safe.com"
        s._on_page_frame_navigated({"frame": "not-a-dict"})
        # 不应抛异常，事件已记录
        assert s.events_count == 1
        assert s._events[0].url == "https://safe.com"

    def test_on_page_load_event_fired(self):
        """_on_page_load_event_fired 写入 navigation 事件，使用 _last_page_url。"""
        s = CDPRecordingSession("rec-load")
        s._last_page_url = "https://loaded.com"
        s._on_page_load_event_fired({})
        ev = s._events[0]
        assert ev.event_type == "navigation"
        assert ev.url == "https://loaded.com"
        assert ev.value == "https://loaded.com"

    def test_on_runtime_console_api_called_click(self):
        """__CDP_RECORDER__ click payload 被正确转换为 click 事件。"""
        s = CDPRecordingSession("rec-click")
        s._last_page_url = "https://p.com"
        s._last_page_title = "P"
        payload = (
            '{"__recorder_type__":"click",'
            '"selector":"#btn","tag":"BUTTON","text":"OK"}'
        )
        s._on_runtime_console_api_called(
            {"args": [{"value": "__CDP_RECORDER__:" + payload}]}
        )
        assert s.events_count == 1
        ev = s._events[0]
        assert ev.event_type == "click"
        assert ev.selector == "#btn"
        # url / page_title 通过 setdefault 回填
        assert ev.url == "https://p.com"
        assert ev.page_title == "P"

    def test_on_runtime_console_api_called_explicit_url_wins(self):
        """payload 含 url 时优先生效，不被 _last_page_url 覆盖。"""
        s = CDPRecordingSession("rec-console-url")
        s._last_page_url = "https://stale.com"
        payload = (
            '{"__recorder_type__":"click","url":"https://fresh.com"}'
        )
        s._on_runtime_console_api_called(
            {"args": [{"value": "__CDP_RECORDER__:" + payload}]}
        )
        assert s._events[0].url == "https://fresh.com"

    def test_on_runtime_console_api_called_ignores_non_recorder(self):
        """普通 console.log（非 __CDP_RECORDER__ 前缀）被忽略。"""
        s = CDPRecordingSession("rec-console-normal")
        s._on_runtime_console_api_called(
            {"args": [{"value": "normal log line"}]}
        )
        # 非 recorder 日志不产生事件
        assert s.events_count == 0

    def test_on_runtime_console_api_called_ignores_non_string_value(self):
        """args[].value 非字符串时跳过该 arg。"""
        s = CDPRecordingSession("rec-console-num")
        s._on_runtime_console_api_called(
            {"args": [{"value": 42}, {"value": None}, {"value": {"x": 1}}]}
        )
        assert s.events_count == 0

    def test_on_runtime_console_api_called_invalid_rec_type(self):
        """__recorder_type__ 不在白名单内 → 跳过。"""
        s = CDPRecordingSession("rec-console-bad-type")
        payload = '{"__recorder_type__":"scroll","selector":"#x"}'
        s._on_runtime_console_api_called(
            {"args": [{"value": "__CDP_RECORDER__:" + payload}]}
        )
        assert s.events_count == 0

    def test_on_runtime_console_api_called_malformed_json(self):
        """__CDP_RECORDER__ 后是无效 JSON 时被静默忽略（不抛异常）。"""
        s = CDPRecordingSession("rec-console-bad-json")
        s._on_runtime_console_api_called(
            {"args": [{"value": "__CDP_RECORDER__:not-json{{{"}]}
        )
        assert s.events_count == 0

    def test_on_runtime_console_api_called_empty_args(self):
        """args 为空 / 缺失时不抛异常。"""
        s = CDPRecordingSession("rec-console-empty")
        s._on_runtime_console_api_called({})  # args 缺失
        s._on_runtime_console_api_called({"args": []})  # args 为空
        assert s.events_count == 0

    def test_on_runtime_console_api_called_unexpected_structure(self):
        """args 中是 dict-like 元素（无 'value' 键）时不抛异常。"""
        s = CDPRecordingSession("rec-console-shape")
        s._on_runtime_console_api_called({"args": [{"type": "string"}]})
        assert s.events_count == 0

    @pytest.mark.asyncio
    async def test_dispatch_cdp_message_routes_methods(self):
        """_dispatch_cdp_message 按 method 分发到对应 handler。"""
        s = CDPRecordingSession("rec-dispatch")

        # Page.frameNavigated
        await s._dispatch_cdp_message(
            {
                "method": "Page.frameNavigated",
                "params": {"frame": {"url": "https://a.com"}},
            }
        )
        # Page.loadEventFired
        s._last_page_url = "https://a.com"
        await s._dispatch_cdp_message(
            {"method": "Page.loadEventFired", "params": {}}
        )
        # Runtime.consoleAPICalled
        s._last_page_url = "https://a.com"
        await s._dispatch_cdp_message(
            {
                "method": "Runtime.consoleAPICalled",
                "params": {
                    "args": [
                        {
                            "value": (
                                "__CDP_RECORDER__:"
                                '{"__recorder_type__":"click","selector":"#y"}'
                            )
                        }
                    ]
                },
            }
        )

        assert s.events_count == 3
        assert [e.event_type for e in s._events] == [
            "navigation",
            "navigation",
            "click",
        ]

    @pytest.mark.asyncio
    async def test_dispatch_cdp_message_unknown_method_ignored(self):
        """未知 method 被静默忽略，不抛异常。"""
        s = CDPRecordingSession("rec-dispatch-unknown")
        await s._dispatch_cdp_message({"method": "Network.requestWillBeSent", "params": {}})
        await s._dispatch_cdp_message({"method": "", "params": {}})  # 空 method
        await s._dispatch_cdp_message({"params": {}})  # 缺 method
        assert s.events_count == 0


# ===========================================================================
# TestHelpers
# ===========================================================================


class TestHelpers:
    """辅助方法：``_extract_cdp_url``、``_next_msg_id``。"""

    def test_extract_cdp_url_from_text(self):
        """tool_result.text 以 ws 开头 → 返回去除前后空白的字符串。"""
        url = CDPRecordingSession._extract_cdp_url(
            {"text": "ws://127.0.0.1:9222/devtools/browser/abc  "}
        )
        assert url == "ws://127.0.0.1:9222/devtools/browser/abc"

    def test_extract_cdp_url_from_url_key(self):
        """从 url / wsUrl / webSocketDebuggerUrl / cdp_url 任一 key 提取。"""
        for key in ("url", "wsUrl", "webSocketDebuggerUrl", "cdp_url"):
            result = CDPRecordingSession._extract_cdp_url(
                {key: "ws://example.com/devtools"}
            )
            assert result == "ws://example.com/devtools", f"key={key}"

    def test_extract_cdp_url_text_not_starting_with_ws_falls_through(self):
        """text 存在但不以 ws 开头时继续尝试 url 等 key。"""
        result = CDPRecordingSession._extract_cdp_url(
            {"text": "http://not-a-ws-url", "url": "ws://real.com"}
        )
        assert result == "ws://real.com"

    def test_extract_cdp_url_returns_none_for_non_dict(self):
        """非 dict 输入 → None。"""
        for bad in (None, "string", 42, ["list"], object()):
            assert CDPRecordingSession._extract_cdp_url(bad) is None

    def test_extract_cdp_url_returns_none_for_unrecognised_keys(self):
        """key 值不是 ws:// 开头 → None。"""
        for bad_key in ("url", "wsUrl", "webSocketDebuggerUrl", "cdp_url"):
            assert CDPRecordingSession._extract_cdp_url(
                {bad_key: "http://not-ws"}
            ) is None
        # text 不以 ws 开头 + 其他 key 不存在 → None
        assert CDPRecordingSession._extract_cdp_url(
            {"text": "no scheme", "other": "value"}
        ) is None

    def test_extract_cdp_url_ignores_non_string_value(self):
        """对应 key 的值不是字符串 → 不被采纳。"""
        for bad_key in ("url", "wsUrl", "webSocketDebuggerUrl", "cdp_url"):
            assert CDPRecordingSession._extract_cdp_url(
                {bad_key: 12345}
            ) is None

    def test_next_msg_id_monotonic(self):
        """_next_msg_id 严格递增。"""
        s = CDPRecordingSession("rec-ids")
        ids = [s._next_msg_id() for _ in range(5)]
        assert ids == [1, 2, 3, 4, 5]
        # 严格单调
        for prev, nxt in zip(ids, ids[1:]):
            assert nxt > prev

    def test_next_msg_id_independent_per_session(self):
        """不同 session 的计数器互不影响。"""
        a = CDPRecordingSession("rec-a")
        b = CDPRecordingSession("rec-b")
        a._next_msg_id()
        a._next_msg_id()
        b._next_msg_id()
        assert a._msg_counter == 2
        assert b._msg_counter == 1


# ===========================================================================
# TestSendCdpAndSafeDetach
# ===========================================================================


class TestSendCdpAndSafeDetach:
    """``_send_cdp`` / ``_safe_detach`` 在无 CDP 传输时的行为。"""

    @pytest.mark.asyncio
    async def test_send_cdp_raises_when_no_transport(self):
        """没有任何活跃传输时 _send_cdp 抛出 RuntimeError。"""
        s = CDPRecordingSession("rec-send")
        with pytest.raises(RuntimeError, match="no active CDP transport"):
            await s._send_cdp("Page.enable", {})

    @pytest.mark.asyncio
    async def test_send_cdp_uses_websocket_transport(self):
        """_ws 已设置但 _cdp_session 为 None 时走 WS 路径（不抛异常）。"""
        s = CDPRecordingSession("rec-send-ws")

        class _FakeWS:
            def __init__(self):
                self.sent: list[str] = []

            async def send(self, msg: str) -> None:
                self.sent.append(msg)

        s._ws = _FakeWS()
        result = await s._send_cdp("Page.enable", {})
        # WS 路径是 fire-and-forget，返回空 dict
        assert result == {}
        assert s._ws.sent and len(s._ws.sent) == 1
        # 验证发送的消息结构
        import json as _json

        sent_payload = _json.loads(s._ws.sent[0])
        assert sent_payload["method"] == "Page.enable"
        assert sent_payload["params"] == {}
        assert sent_payload["id"] == 1

    @pytest.mark.asyncio
    async def test_send_cdp_uses_cdp_session_transport(self):
        """_cdp_session 存在时优先走 Playwright CDP 路径。"""
        s = CDPRecordingSession("rec-send-cdp")

        class _FakeCDP:
            def __init__(self):
                self.sent: list[tuple[str, dict]] = []

            async def send(self, method: str, params: dict) -> dict:
                self.sent.append((method, params))
                return {"ok": True}

        fake = _FakeCDP()
        s._cdp_session = fake
        result = await s._send_cdp("Runtime.enable", {"foo": "bar"})
        assert result == {"ok": True}
        assert fake.sent == [("Runtime.enable", {"foo": "bar"})]

    @pytest.mark.asyncio
    async def test_send_cdp_coerces_non_dict_result(self):
        """_cdp_session.send 返回非 dict 时 _send_cdp 返回空 dict。"""
        s = CDPRecordingSession("rec-send-coerce")

        class _FakeCDP:
            async def send(self, method: str, params: dict):
                return "string-result"

        s._cdp_session = _FakeCDP()
        result = await s._send_cdp("Page.enable", {})
        assert result == {}

    @pytest.mark.asyncio
    async def test_safe_detach_with_no_resources(self):
        """无任何资源时 _safe_detach 是 no-op，不抛异常。"""
        s = CDPRecordingSession("rec-detach-empty")
        # 全部为 None
        await s._safe_detach()
        # 状态保持
        assert s._cdp_session is None
        assert s._ws is None
        assert s._reader_task is None
        assert s._cdp_url is None
        assert s._attached_manager is None

    @pytest.mark.asyncio
    async def test_safe_detach_cancels_reader_task(self):
        """_reader_task 存在时 _safe_detach 取消并清空它。"""
        s = CDPRecordingSession("rec-detach-task")

        async def _never():
            await asyncio.sleep(3600)

        s._reader_task = asyncio.create_task(_never())
        # 给一点时间让 task 启动
        await asyncio.sleep(0)
        await s._safe_detach()
        assert s._reader_task is None

    @pytest.mark.asyncio
    async def test_safe_detach_closes_websocket(self):
        """_ws 存在时调用 close()。"""
        s = CDPRecordingSession("rec-detach-ws")

        class _FakeWS:
            def __init__(self):
                self.closed = False

            async def close(self) -> None:
                self.closed = True

        ws = _FakeWS()
        s._ws = ws
        await s._safe_detach()
        assert ws.closed is True
        assert s._ws is None

    @pytest.mark.asyncio
    async def test_safe_detach_detaches_cdp_session_sync(self):
        """_cdp_session.detach 是同步函数时也应被调用。"""
        s = CDPRecordingSession("rec-detach-cdp-sync")

        class _FakeCDP:
            def __init__(self):
                self.detached = False

            def detach(self):
                self.detached = True

        fake = _FakeCDP()
        s._cdp_session = fake
        await s._safe_detach()
        assert fake.detached is True
        assert s._cdp_session is None

    @pytest.mark.asyncio
    async def test_safe_detach_detaches_cdp_session_async(self):
        """_cdp_session.detach 是 async 函数时也应被 await。"""
        s = CDPRecordingSession("rec-detach-cdp-async")

        class _FakeCDP:
            def __init__(self):
                self.detached = False

            async def detach(self):
                self.detached = True

        fake = _FakeCDP()
        s._cdp_session = fake
        await s._safe_detach()
        assert fake.detached is True
        assert s._cdp_session is None

    @pytest.mark.asyncio
    async def test_safe_detach_handles_cdp_session_without_detach(self):
        """_cdp_session 没有 detach 方法时也不抛异常。"""
        s = CDPRecordingSession("rec-detach-nomethod")

        class _FakeCDP:
            pass

        s._cdp_session = _FakeCDP()
        await s._safe_detach()
        assert s._cdp_session is None

    @pytest.mark.asyncio
    async def test_safe_detach_resets_attached_manager(self):
        """_safe_detach 总是清空 _attached_manager 与 _cdp_url。"""
        s = CDPRecordingSession("rec-detach-mgr")
        s._attached_manager = object()
        s._cdp_url = "ws://something"
        await s._safe_detach()
        assert s._attached_manager is None
        assert s._cdp_url is None

    @pytest.mark.asyncio
    async def test_safe_detach_handles_reader_cancel_exception(self):
        """_reader_task.cancel() 抛异常时 _safe_detach 静默忽略。"""
        s = CDPRecordingSession("rec-detach-task-exc")

        class _BadTask:
            def cancel(self):
                raise RuntimeError("cancel failed")

        s._reader_task = _BadTask()
        # 不应抛异常
        await s._safe_detach()
        # 错误路径下 _reader_task 仍会被置为 None（来自赋值）

    @pytest.mark.asyncio
    async def test_safe_detach_handles_ws_close_exception(self):
        """_ws.close() 抛异常时 _safe_detach 静默忽略。"""
        s = CDPRecordingSession("rec-detach-ws-exc")

        class _BadWS:
            async def close(self):
                raise RuntimeError("close failed")

        s._ws = _BadWS()
        # 不应抛异常
        await s._safe_detach()
        assert s._ws is None

    @pytest.mark.asyncio
    async def test_safe_detach_handles_cdp_detach_exception(self):
        """_cdp_session.detach() 抛异常时 _safe_detach 静默忽略。"""
        s = CDPRecordingSession("rec-detach-cdp-exc")

        class _BadCDP:
            def detach(self):
                raise RuntimeError("detach failed")

        s._cdp_session = _BadCDP()
        # 不应抛异常
        await s._safe_detach()
        assert s._cdp_session is None

    @pytest.mark.asyncio
    async def test_safe_detach_handles_cdp_detach_returning_coro(self):
        """_cdp_session.detach() 返回 coroutine 时也安全 await。"""
        s = CDPRecordingSession("rec-detach-cdp-coro")

        class _CDP:
            def __init__(self):
                self.detached = False

            def detach(self):
                async def _coro():
                    self.detached = True

                return _coro()

        fake = _CDP()
        s._cdp_session = fake
        await s._safe_detach()
        assert fake.detached is True


# ===========================================================================
# TestDomainManagement
# ===========================================================================


class TestDomainManagement:
    """``_enable_domains`` / ``_disable_domains`` / ``_install_page_recorder``。"""

    @pytest.mark.asyncio
    async def test_enable_domains_calls_three(self):
        """_enable_domains 依次启用 Page、Runtime、Network。"""
        s = CDPRecordingSession("rec-enable")
        with patch.object(
            s, "_send_cdp", new=AsyncMock(return_value={})
        ) as mock_send:
            await s._enable_domains()
        methods = [call.args[0] for call in mock_send.call_args_list]
        assert methods == ["Page.enable", "Runtime.enable", "Network.enable"]

    @pytest.mark.asyncio
    async def test_enable_domains_swallows_exceptions(self):
        """某个域启用失败时不影响其他域的启用。"""
        s = CDPRecordingSession("rec-enable-fail")

        async def _selective(method, params):
            if method == "Runtime.enable":
                raise RuntimeError("runtime domain broke")
            return {}

        with patch.object(s, "_send_cdp", new=AsyncMock(side_effect=_selective)):
            # 不应抛异常
            await s._enable_domains()

    @pytest.mark.asyncio
    async def test_disable_domains_calls_three(self):
        """_disable_domains 依次禁用 Page、Runtime、Network。"""
        s = CDPRecordingSession("rec-disable")
        with patch.object(
            s, "_send_cdp", new=AsyncMock(return_value={})
        ) as mock_send:
            await s._disable_domains()
        methods = [call.args[0] for call in mock_send.call_args_list]
        assert methods == ["Page.disable", "Runtime.disable", "Network.disable"]

    @pytest.mark.asyncio
    async def test_disable_domains_swallows_exceptions(self):
        """_disable_domains 总是 best-effort，异常不向上抛。"""
        s = CDPRecordingSession("rec-disable-fail")

        async def _fail_all(method, params):
            raise ConnectionError("ws closed")

        with patch.object(s, "_send_cdp", new=AsyncMock(side_effect=_fail_all)):
            await s._disable_domains()  # 不应抛

    @pytest.mark.asyncio
    async def test_install_page_recorder_sends_script(self):
        """_install_page_recorder 注入 _INJECT_RECORDER_SCRIPT（两次 CDP 调用）。"""
        from core.cdp_session import _INJECT_RECORDER_SCRIPT

        s = CDPRecordingSession("rec-install")
        with patch.object(
            s, "_send_cdp", new=AsyncMock(return_value={})
        ) as mock_send:
            await s._install_page_recorder()
        assert mock_send.call_count == 2
        first_call = mock_send.call_args_list[0]
        second_call = mock_send.call_args_list[1]
        method1, params1 = first_call.args
        method2, params2 = second_call.args
        assert method1 == "Page.addScriptToEvaluateOnNewDocument"
        assert params1 == {"source": _INJECT_RECORDER_SCRIPT}
        assert method2 == "Runtime.evaluate"
        assert params2 == {"expression": _INJECT_RECORDER_SCRIPT, "awaitPromise": False}

    @pytest.mark.asyncio
    async def test_install_page_recorder_swallows_exceptions(self):
        """注入失败时 recording 仍可继续（不抛异常）。"""
        s = CDPRecordingSession("rec-install-fail")

        async def _fail(method, params):
            raise RuntimeError("injection failed")

        with patch.object(s, "_send_cdp", new=AsyncMock(side_effect=_fail)):
            await s._install_page_recorder()  # 不应抛


# ===========================================================================
# TestAttachCDP
# ===========================================================================


class TestAttachCDP:
    """``_attach_cdp`` 三个分支：PlaywrightMCPManager / str / Playwright Page。"""

    @pytest.mark.asyncio
    async def test_attach_cdp_none_returns_false(self):
        """target=None 立即返回 False。"""
        s = CDPRecordingSession("rec-attach-none")
        assert await s._attach_cdp(None) is False

    @pytest.mark.asyncio
    async def test_attach_cdp_mcp_manager_success(self):
        """target 是 PlaywrightMCPManager-like 对象：call_tool 返回 ws URL → 成功。"""
        s = CDPRecordingSession("rec-attach-mcp")

        class _MCPManager:
            def __init__(self):
                self.session = MagicMock()
                self.call_tool = AsyncMock(
                    return_value={"text": "ws://127.0.0.1:9222/devtools/browser/xyz"}
                )

        mgr = _MCPManager()

        # Mock _open_websocket 让其成功设置 _ws
        async def _fake_open(url):
            class _WS:
                pass

            s._ws = _WS()

        with patch.object(s, "_open_websocket", new=AsyncMock(side_effect=_fake_open)):
            result = await s._attach_cdp(mgr)

        assert result is True
        assert s._attached_manager is mgr
        assert s._cdp_url == "ws://127.0.0.1:9222/devtools/browser/xyz"
        mgr.call_tool.assert_awaited_once_with("browser_cdp_session", {})

    @pytest.mark.asyncio
    async def test_attach_cdp_mcp_manager_no_cdp_url(self):
        """call_tool 返回值不含 ws URL → 返回 False。"""
        s = CDPRecordingSession("rec-attach-mcp-nourl")

        class _MCPManager:
            session = MagicMock()
            call_tool = AsyncMock(return_value={"success": False, "text": ""})

        mgr = _MCPManager()
        result = await s._attach_cdp(mgr)
        assert result is False

    @pytest.mark.asyncio
    async def test_attach_cdp_mcp_manager_call_tool_exception(self):
        """call_tool 抛异常 → 返回 False（不向上抛）。"""
        s = CDPRecordingSession("rec-attach-mcp-exc")

        class _MCPManager:
            session = MagicMock()
            call_tool = AsyncMock(side_effect=RuntimeError("MCP died"))

        mgr = _MCPManager()
        result = await s._attach_cdp(mgr)
        assert result is False

    @pytest.mark.asyncio
    async def test_attach_cdp_str_uses_open_websocket(self):
        """str target → 调用 _open_websocket，_cdp_url 被设置。"""
        s = CDPRecordingSession("rec-attach-str")

        async def _fake_open(url):
            s._ws = MagicMock()

        with patch.object(s, "_open_websocket", new=AsyncMock(side_effect=_fake_open)):
            result = await s._attach_cdp("ws://localhost:9222/devtools/browser/abc")

        assert result is True
        assert s._cdp_url == "ws://localhost:9222/devtools/browser/abc"

    @pytest.mark.asyncio
    async def test_attach_cdp_str_open_websocket_fails(self):
        """_open_websocket 抛异常 → 返回 False。"""
        s = CDPRecordingSession("rec-attach-str-fail")

        async def _bad_open(url):
            raise ConnectionError("cannot connect")

        with patch.object(s, "_open_websocket", new=AsyncMock(side_effect=_bad_open)):
            result = await s._attach_cdp("ws://bad:1/devtools")

        assert result is False

    @pytest.mark.asyncio
    async def test_attach_cdp_playwright_page_success(self):
        """Playwright Page-like target → 走 new_cdp_session 分支。"""
        s = CDPRecordingSession("rec-attach-pw")

        class _FakeCDP:
            def __init__(self):
                self.handlers: list[tuple[str, object]] = []

            def on(self, method, handler):
                self.handlers.append((method, handler))

        fake_cdp = _FakeCDP()

        class _FakePage:
            def __init__(self):
                self.url = "https://example.com/"

            async def title(self):
                return "Example"

            async def evaluate(self, _script):
                return None

            class context:
                @staticmethod
                async def new_cdp_session(_page):
                    return fake_cdp

        result = await s._attach_cdp(_FakePage())

        assert result is True
        assert s._cdp_session is fake_cdp
        registered_methods = [m for m, _ in fake_cdp.handlers]
        assert "Page.frameNavigated" in registered_methods
        assert "Page.loadEventFired" in registered_methods
        assert "Runtime.consoleAPICalled" in registered_methods
        assert s._last_page_url == "https://example.com/"
        assert s._last_page_title == "Example"

    @pytest.mark.asyncio
    async def test_attach_cdp_playwright_page_initial_url_fail_silent(self):
        """首次 page.url/title 抓取失败时静默回退（不抛异常）。"""
        s = CDPRecordingSession("rec-attach-pw-fail")

        class _FakeCDP:
            def on(self, method, handler):
                pass

        class _BadPage:
            def __init__(self):
                self.context = MagicMock()
                self.context.new_cdp_session = AsyncMock(return_value=_FakeCDP())

            @property
            def url(self):
                raise RuntimeError("url read failed")

            async def title(self):
                raise RuntimeError("title read failed")

            async def evaluate(self, _script):
                return None

        result = await s._attach_cdp(_BadPage())
        assert result is True
        assert s._last_page_url == ""
        assert s._last_page_title == ""

    @pytest.mark.asyncio
    async def test_attach_cdp_playwright_page_creation_fails(self):
        """new_cdp_session 抛异常 → 返回 False。"""
        s = CDPRecordingSession("rec-attach-pw-exc")

        class _BadPage:
            class context:
                @staticmethod
                async def new_cdp_session(_page):
                    raise RuntimeError("CDP session failed")

            async def evaluate(self, _script):
                return None

        result = await s._attach_cdp(_BadPage())
        assert result is False

    @pytest.mark.asyncio
    async def test_attach_cdp_unsupported_target(self):
        """既非 manager/str/Page 的对象 → 返回 False。"""
        s = CDPRecordingSession("rec-attach-unsupported")
        assert await s._attach_cdp(42) is False
        assert await s._attach_cdp([1, 2, 3]) is False
        assert await s._attach_cdp(object()) is False


# ===========================================================================
# TestOpenWebSocket
# ===========================================================================


class TestOpenWebSocket:
    """``_open_websocket`` 使用 websockets 库打开真实连接。"""

    @pytest.mark.asyncio
    async def test_open_websocket_success(self):
        """成功路径：_ws 被设置，_reader_task 被创建。"""
        s = CDPRecordingSession("rec-ws-open")

        class _FakeWS:
            pass

        class _FakeWebsocketsModule:
            @staticmethod
            async def connect(url, **kwargs):
                assert url == "ws://localhost:9222"
                assert kwargs.get("max_size") == 32 * 1024 * 1024
                return _FakeWS()

        import sys as _sys

        real_websockets = _sys.modules.get("websockets")
        _sys.modules["websockets"] = _FakeWebsocketsModule
        try:
            await s._open_websocket("ws://localhost:9222")
        finally:
            if real_websockets is not None:
                _sys.modules["websockets"] = real_websockets
            else:
                _sys.modules.pop("websockets", None)

        assert isinstance(s._ws, _FakeWS)
        assert s._reader_task is not None
        # 清理后台 task
        s._reader_task.cancel()
        try:
            await s._reader_task
        except BaseException:
            pass

    @pytest.mark.asyncio
    async def test_open_websocket_import_error_raises_runtime(self):
        """websockets 库不可用时 _open_websocket 抛出 RuntimeError。"""
        s = CDPRecordingSession("rec-ws-no-lib")

        import sys as _sys

        # 把 websockets 从 sys.modules 中移除并阻止重新导入
        real_websockets = _sys.modules.pop("websockets", None)
        # 注入一个 ImportError 的 finder
        class _BlockWebsockets:
            def find_module(self, name, path=None):
                if name == "websockets" or name.startswith("websockets."):
                    return self
                return None

            def load_module(self, name):
                raise ImportError(f"blocked: {name}")

        meta_path = _sys.meta_path
        blocker = _BlockWebsockets()
        meta_path.insert(0, blocker)
        try:
            with pytest.raises(RuntimeError, match="websockets package is required"):
                await s._open_websocket("ws://localhost:9222")
        finally:
            meta_path.remove(blocker)
            if real_websockets is not None:
                _sys.modules["websockets"] = real_websockets


# ===========================================================================
# TestStartStopRecording
# ===========================================================================


class TestStartStopRecording:
    """``start_recording`` / ``stop_recording`` 完整成功路径（mock 内部依赖）。"""

    @pytest.mark.asyncio
    async def test_start_recording_success(self):
        """所有依赖成功时 start_recording 返回 True，状态变为 recording。"""
        s = CDPRecordingSession("rec-start-ok")

        with patch.object(s, "_attach_cdp", new=AsyncMock(return_value=True)), \
             patch.object(s, "_enable_domains", new=AsyncMock()), \
             patch.object(s, "_install_page_recorder", new=AsyncMock()):
            result = await s.start_recording("ws://127.0.0.1:9222")

        assert result is True
        assert s.is_recording is True
        assert s._start_time is not None
        assert s._start_time <= time.time()

    @pytest.mark.asyncio
    async def test_start_recording_attach_fails_returns_false(self):
        """_attach_cdp 返回 False 时 start_recording 返回 False。"""
        s = CDPRecordingSession("rec-start-attachfail")

        with patch.object(s, "_attach_cdp", new=AsyncMock(return_value=False)), \
             patch.object(s, "_enable_domains", new=AsyncMock()) as mock_enable, \
             patch.object(s, "_install_page_recorder", new=AsyncMock()) as mock_install:
            result = await s.start_recording("ws://bad:1/devtools")

        assert result is False
        assert s.is_recording is False
        # 后续步骤不应执行
        mock_enable.assert_not_awaited()
        mock_install.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_start_recording_inner_exception_calls_safe_detach(self):
        """enable_domains 抛异常时 _safe_detach 被调用，状态回滚。"""
        s = CDPRecordingSession("rec-start-exc")

        with patch.object(s, "_attach_cdp", new=AsyncMock(return_value=True)), \
             patch.object(s, "_enable_domains",
                          new=AsyncMock(side_effect=RuntimeError("boom"))), \
             patch.object(s, "_safe_detach", new=AsyncMock()) as mock_detach:
            result = await s.start_recording("ws://127.0.0.1:9222")

        assert result is False
        assert s.is_recording is False
        assert s._start_time is None
        mock_detach.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_stop_recording_when_recording(self):
        """recording 状态下 stop_recording 走完整清理路径。"""
        s = CDPRecordingSession("rec-stop-active")
        s._recording = True
        s._start_time = time.time() - 1.5
        s.record_event({"event_type": "click", "url": "https://x.com"})

        with patch.object(s, "_disable_domains", new=AsyncMock()) as mock_disable, \
             patch.object(s, "_safe_detach", new=AsyncMock()) as mock_detach:
            result = await s.stop_recording()

        assert result is True
        assert s.is_recording is False
        # _disable_domains 和 _safe_detach 均被调用
        mock_disable.assert_awaited_once()
        mock_detach.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_stop_recording_disable_domains_exception_continues(self):
        """_disable_domains 抛异常时 stop_recording 仍应完成清理。"""
        s = CDPRecordingSession("rec-stop-disable-exc")
        s._recording = True

        with patch.object(s, "_disable_domains",
                          new=AsyncMock(side_effect=RuntimeError("disable fail"))), \
             patch.object(s, "_safe_detach", new=AsyncMock()) as mock_detach:
            result = await s.stop_recording()

        assert result is True
        assert s.is_recording is False
        # _safe_detach 仍应被调用
        mock_detach.assert_awaited_once()


# ===========================================================================
# TestReadWsLoop
# ===========================================================================


class TestReadWsLoop:
    """``_read_ws_loop`` 处理消息循环逻辑（mock websockets）。"""

    @pytest.mark.asyncio
    async def test_read_ws_loop_none_ws_returns(self):
        """_ws 为 None 时立即返回。"""
        s = CDPRecordingSession("rec-read-none")
        s._ws = None
        # 不应抛异常或 hang
        await s._read_ws_loop()

    @pytest.mark.asyncio
    async def test_read_ws_loop_processes_messages(self):
        """读取并 dispatch 消息。"""
        s = CDPRecordingSession("rec-read-msgs")

        class _FakeWS:
            def __init__(self, messages):
                self._messages = list(messages)

            def __aiter__(self):
                return self

            async def __anext__(self):
                if not self._messages:
                    raise StopAsyncIteration
                return self._messages.pop(0)

        messages = [
            '{"method":"Page.frameNavigated","params":{"frame":{"url":"https://a.com"}}}',
            '{"method":"Page.loadEventFired","params":{}}',
            "not-json-{{{",  # 无效 JSON 应被忽略
            '{"unknown":"shape"}',  # 缺 method,被忽略
        ]
        s._ws = _FakeWS(messages)
        s._last_page_url = "https://a.com"
        # 不应抛异常
        await s._read_ws_loop()
        # 2 条有效消息产生 2 个 navigation 事件
        assert s.events_count == 2
        assert all(e.event_type == "navigation" for e in s._events)

    @pytest.mark.asyncio
    async def test_read_ws_loop_handles_iteration_exception(self):
        """websocket 迭代抛非 CancelledError 异常时被 catch,日志记录,不传播。"""
        s = CDPRecordingSession("rec-read-iter-exc")

        class _BadWS:
            def __aiter__(self):
                return self

            async def __anext__(self):
                raise RuntimeError("ws broke")

        s._ws = _BadWS()
        # 不应向上传播
        await s._read_ws_loop()
