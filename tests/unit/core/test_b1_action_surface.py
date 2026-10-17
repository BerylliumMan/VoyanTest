"""B1 动作面全量接线：新增动作的 MCP 参数构造与门控。"""
from __future__ import annotations

import json

import pytest

from core.mcp_args import (
    NON_COUNTING_ACTIONS,
    SIDE_EFFECT_ACTIONS,
    TOOL_MAP,
    action_disabled_reason,
    build_mcp_args,
    resolve_mcp_tool,
)


class TestToolMapCoverage:
    """新动作 → MCP 工具名一一映射。"""

    @pytest.mark.parametrize(
        "action,tool",
        [
            ("fill_form", "browser_fill_form"),
            ("network", "browser_network_requests"),
            ("network_request", "browser_network_request"),
            ("resize", "browser_resize"),
            ("emulate_media", "browser_emulate_media"),
            ("mouse_down", "browser_mouse_down"),
            ("mouse_up", "browser_mouse_up"),
            ("close", "browser_close"),
            ("pdf", "browser_pdf_save"),
            ("video_start", "browser_start_video"),
            ("video_stop", "browser_stop_video"),
            ("trace_start", "browser_start_tracing"),
            ("trace_stop", "browser_stop_tracing"),
            ("recording_start", "browser_start_recording"),
            ("recording_stop", "browser_stop_recording"),
            ("run_code", "browser_run_code_unsafe"),
            # check/uncheck 无原生 MCP 工具（0.0.83 实测），语义=点击切换
            ("check", "browser_click"),
            ("uncheck", "browser_click"),
        ],
    )
    def test_new_actions_mapped(self, action, tool):
        assert resolve_mcp_tool(action) == tool

    def test_side_effect_set_includes_new_risky(self):
        for name in ("fill_form", "mouse_down", "mouse_up", "close", "run_code"):
            assert name in SIDE_EFFECT_ACTIONS

    def test_evidence_actions_non_counting(self):
        for name in ("network", "resize", "pdf", "video_start", "trace_stop", "console"):
            assert name in NON_COUNTING_ACTIONS


class TestBuildMcpArgs:
    """参数构造（value 语法契约）。"""

    def _build(self, action, value=None, selector=None):
        return build_mcp_args(action, selector=selector, element_desc=None, value=value)

    def test_wait_numeric_value_as_int(self):
        # 模型偶发输出未加引号的数字（wait 1）——此前 .isdigit() 崩溃
        assert self._build("wait", 1) == {"time": 1}
        assert self._build("wait", "2") == {"time": 2}
        assert self._build("wait", "提示文本") == {"text": "提示文本"}
        assert self._build("wait", "3", None) == {"time": 3}
        assert build_mcp_args("wait", selector=None, element_desc=None, value="5", wait_mode="gone") == {"time": 5}

    def test_fill_form_json_array(self):
        fields = [
            {"target": "e11", "name": "用户名", "type": "textbox", "value": "admin"},
            {"target": "e13", "name": "密码", "type": "textbox", "value": "```"},
        ]
        args = self._build("fill_form", json.dumps(fields, ensure_ascii=False))
        assert len(args["fields"]) == 2
        assert args["fields"][0]["target"] == "e11"
        assert args["fields"][0]["type"] == "textbox"
        assert args["fields"][1]["value"] == "```"

    def test_fill_form_dict_shorthand(self):
        args = self._build("fill_form", json.dumps({"e11": "admin", "e13": "pw"}))
        by_target = {f["target"]: f for f in args["fields"]}
        assert by_target["e11"]["value"] == "admin"
        assert by_target["e13"]["type"] == "textbox"

    def test_fill_form_invalid_json_returns_empty(self):
        assert self._build("fill_form", "not json")["fields"] == []

    def test_fill_form_bad_type_falls_back_textbox(self):
        fields = [{"target": "e1", "name": "x", "type": "hacked", "value": "v"}]
        args = self._build("fill_form", json.dumps(fields))
        assert args["fields"][0]["type"] == "textbox"

    def test_network_modes(self):
        assert self._build("network", "") == {"static": False}
        assert self._build("network", "static") == {"static": True}
        assert self._build("network", "/api/.*") == {"static": False, "filter": "/api/.*"}

    def test_network_request_index_and_part(self):
        assert self._build("network_request", "3") == {"index": 3}
        assert self._build("network_request", "5:response-body") == {
            "index": 5,
            "part": "response-body",
        }
        assert self._build("network_request", "abc") == {}

    def test_resize(self):
        assert self._build("resize", "1280x720") == {"width": 1280, "height": 720}
        assert self._build("resize", "375,812") == {"width": 375, "height": 812}
        assert self._build("resize", "bad") == {}

    def test_emulate_media(self):
        assert self._build("emulate_media", "dark") == {"colorScheme": "dark"}
        assert self._build("emulate_media", "print") == {"media": "print"}
        assert self._build("emulate_media", '{"contrast": "more"}') == {"contrast": "more"}

    def test_mouse_buttons(self):
        assert self._build("mouse_down") == {"button": "left"}
        assert self._build("mouse_up", "right") == {"button": "right"}

    def test_pdf_close_media_controls(self):
        assert self._build("pdf") == {}
        assert self._build("pdf", "report.pdf") == {"filename": "report.pdf"}
        assert self._build("close") == {}
        assert self._build("trace_start") == {}
        assert self._build("recording_stop") == {}
        assert self._build("video_stop") == {}

    def test_video_start_filename_or_json(self):
        assert self._build("video_start", "run.webm") == {"filename": "run.webm"}
        args = self._build("video_start", '{"fps": 12, "cursor": true}')
        assert args == {"fps": 12, "cursor": True}

    def test_video_chapter(self):
        assert self._build("video_chapter", "登录流程") == {"title": "登录流程"}
        args = self._build("video_chapter", '{"title": "x", "duration": 800}')
        assert args == {"title": "x", "duration": 800}


class TestRunCodeGate:
    """run_code 开关（2026-10-16 起默认开启；关闭后拦截）。"""

    def test_enabled_by_default(self):
        assert action_disabled_reason("run_code") is None

    def test_disabled_when_switch_off(self, monkeypatch):
        import app.config as app_config

        class _S:
            exec_run_code_enabled = False

        monkeypatch.setattr(app_config, "get_settings", lambda: _S())
        reason = action_disabled_reason("run_code")
        assert reason is not None and "run_code" in reason

    def test_other_actions_unaffected(self):
        assert action_disabled_reason("click") is None
        assert action_disabled_reason("fill_form") is None
        assert action_disabled_reason("") is None
