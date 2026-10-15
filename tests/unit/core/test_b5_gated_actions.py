"""B5：verify 四件套 / 网络模拟 / 存储操作 的参数构造与门控。"""
from __future__ import annotations

import pytest


class TestVerifyArgs:
    def _build(self, action, **kw):
        from core.mcp_args import build_mcp_args

        return build_mcp_args(
            action, selector=kw.pop("selector", None),
            element_desc=kw.pop("element_desc", None), value=kw.pop("value", None),
            **kw,
        )

    def test_verify_text(self):
        assert self._build("verify_text", value="提交成功") == {"text": "提交成功"}

    def test_verify_visible_requires_role(self):
        with pytest.raises(ValueError):
            self._build("verify_visible", value="登录")
        assert self._build("verify_visible", value="登录", role="button") == {
            "role": "button", "accessibleName": "登录",
        }

    def test_verify_value(self):
        out = self._build("verify_value", selector="e12", element_desc="用户名", value="admin")
        assert out["type"] == "textbox" and out["target"] == "e12"
        assert out["element"] == "用户名" and out["value"] == "admin"
        out2 = self._build("verify_value", selector="e1", value="true", field_type="checkbox")
        assert out2["type"] == "checkbox"

    def test_verify_list_json_and_single(self):
        out = self._build("verify_list", selector="e9", value='["A","B"]')
        assert out["items"] == ["A", "B"] and out["target"] == "e9"
        out2 = self._build("verify_list", selector="e9", value="A")
        assert out2["items"] == ["A"]

    def test_tool_map_resolution(self):
        from core.mcp_args import resolve_mcp_tool

        assert resolve_mcp_tool("verify_visible") == "browser_verify_element_visible"
        assert resolve_mcp_tool("verify_text") == "browser_verify_text_visible"
        assert resolve_mcp_tool("verify_value") == "browser_verify_value"
        assert resolve_mcp_tool("verify_list") == "browser_verify_list_visible"


class TestNetworkMockArgs:
    def _build(self, action, **kw):
        from core.mcp_args import build_mcp_args

        return build_mcp_args(
            action, selector=kw.pop("selector", None),
            element_desc=None, value=kw.pop("value", None), **kw,
        )

    def test_route_json(self):
        out = self._build(
            "route",
            value='{"pattern":"**/api/x","status":500,"body":"{}","contentType":"application/json"}',
        )
        assert out == {
            "pattern": "**/api/x", "status": 500, "body": "{}",
            "contentType": "application/json",
        }

    def test_route_requires_pattern(self):
        with pytest.raises(ValueError):
            self._build("route", value='{"status":500}')
        with pytest.raises(ValueError):
            self._build("route", value="not json")

    def test_route_list_and_unroute(self):
        assert self._build("route_list") == {}
        assert self._build("unroute") == {}
        assert self._build("unroute", value="**/api/x") == {"pattern": "**/api/x"}

    def test_network_state_validation(self):
        assert self._build("network_state", value="offline") == {"state": "offline"}
        assert self._build("network_state", value="ONLINE") == {"state": "online"}
        with pytest.raises(ValueError):
            self._build("network_state", value="half")


class TestStorageArgs:
    def _build(self, action, **kw):
        from core.mcp_args import build_mcp_args

        return build_mcp_args(
            action, selector=None, element_desc=None,
            value=kw.pop("value", None), **kw,
        )

    def test_cookie_args(self):
        assert self._build("cookie_get", value="sid") == {"name": "sid"}
        assert self._build("cookie_delete", value="sid") == {"name": "sid"}
        assert self._build("cookie_clear") == {}
        assert self._build("cookie_list") == {}
        assert self._build("cookie_list", value='{"domain":".x.com"}') == {"domain": ".x.com"}
        out = self._build("cookie_set", value='{"name":"a","value":"1","httpOnly":true}')
        assert out == {"name": "a", "value": "1", "httpOnly": True}
        with pytest.raises(ValueError):
            self._build("cookie_set", value='{"value":"1"}')
        with pytest.raises(ValueError):
            self._build("cookie_get", value="")

    def test_localstorage_args(self):
        assert self._build("localstorage_list") == {}
        assert self._build("localstorage_clear") == {}
        assert self._build("localstorage_get", value="token") == {"key": "token"}
        assert self._build("localstorage_delete", value="token") == {"key": "token"}
        assert self._build("localstorage_set", value='{"key":"k","value":"v"}') == {
            "key": "k", "value": "v",
        }
        assert self._build("localstorage_set", value='{"key":"k"}') == {"key": "k", "value": ""}
        with pytest.raises(ValueError):
            self._build("localstorage_get", value="")

    def test_sessionstorage_args(self):
        assert self._build("sessionstorage_list") == {}
        assert self._build("sessionstorage_get", value="k") == {"key": "k"}
        assert self._build("sessionstorage_set", value='{"key":"k","value":"2"}') == {
            "key": "k", "value": "2",
        }

    def test_storage_state_args(self):
        assert self._build("storage_state_save") == {}
        assert self._build("storage_state_save", value="a.json") == {"filename": "a.json"}
        assert self._build("storage_state_restore", value="a.json") == {"filename": "a.json"}
        with pytest.raises(ValueError):
            self._build("storage_state_restore", value="")


class TestGating:
    def test_run_code_gated(self, monkeypatch):
        from core.mcp_args import action_disabled_reason

        class _S:
            exec_run_code_enabled = False
            exec_network_mock_enabled = False
            exec_storage_actions_enabled = False

        import app.config as cfg

        monkeypatch.setattr(cfg, "get_settings", lambda: _S())
        assert action_disabled_reason("run_code")
        assert action_disabled_reason("route")
        assert action_disabled_reason("unroute")
        assert action_disabled_reason("network_state")
        assert action_disabled_reason("cookie_list")
        assert action_disabled_reason("localstorage_get")
        assert action_disabled_reason("storage_state_restore")
        # 非门控动作不受影响
        assert action_disabled_reason("click") is None
        assert action_disabled_reason("verify_text") is None

    def test_gated_enabled(self, monkeypatch):
        from core.mcp_args import action_disabled_reason

        class _S:
            exec_run_code_enabled = True
            exec_network_mock_enabled = True
            exec_storage_actions_enabled = True

        import app.config as cfg

        monkeypatch.setattr(cfg, "get_settings", lambda: _S())
        assert action_disabled_reason("run_code") is None
        assert action_disabled_reason("route") is None
        assert action_disabled_reason("cookie_set") is None

    def test_client_without_config_conservative(self, monkeypatch):
        from core.mcp_args import action_disabled_reason

        import app.config as cfg

        def _boom():
            raise RuntimeError("no config in client process")

        monkeypatch.setattr(cfg, "get_settings", _boom)
        assert action_disabled_reason("run_code")
        assert action_disabled_reason("route")
        assert action_disabled_reason("cookie_list")
        assert action_disabled_reason("click") is None
