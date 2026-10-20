"""037+ 服务端 MCP 启动方式：离线优先（本地 cli.js），npx 仅作回退。

2026-10-20 线上问题：目标服务器无外网，``npx -y @playwright/mcp`` 去
registry.npmjs.org 取包失败（ENOTFOUND），MCP 子进程立即退出 →
``Connection closed``，服务端探索/执行全部不可用。镜像里其实已装好
@playwright/mcp，只是 npx 按包名找不到（全局 bin 名是 playwright-mcp）。
"""
import os

from core.playwright_manager import build_mcp_launch, resolve_mcp_cli


MCP_ARGS = ["--browser=chromium", "--isolated", "--caps=vision"]


def test_resolve_uses_explicit_env(tmp_path, monkeypatch):
    cli = tmp_path / "cli.js"
    cli.write_text("// mcp", encoding="utf-8")
    monkeypatch.setenv("VT_MCP_CLI", str(cli))
    assert resolve_mcp_cli() == str(cli)


def test_resolve_ignores_missing_env(tmp_path, monkeypatch):
    monkeypatch.setenv("VT_MCP_CLI", str(tmp_path / "nope.js"))
    monkeypatch.setattr(os.path, "isfile", lambda p: False)
    assert resolve_mcp_cli() == ""


def test_resolve_finds_global_install(monkeypatch):
    seen = []
    expected = "/usr/lib/node_modules/@playwright/mcp/cli.js"

    def fake_isfile(path):
        seen.append(path)
        return path == expected

    monkeypatch.delenv("VT_MCP_CLI", raising=False)
    monkeypatch.delenv("PLAYWRIGHT_MCP_CLI", raising=False)
    monkeypatch.setattr(os.path, "isfile", fake_isfile)
    assert resolve_mcp_cli() == expected
    assert expected in seen


def test_build_launch_prefers_local_cli(monkeypatch):
    monkeypatch.delenv("VT_NODE_BIN", raising=False)
    command, args = build_mcp_launch("/opt/mcp/cli.js", MCP_ARGS)
    assert command == "node"
    assert args == ["/opt/mcp/cli.js", *MCP_ARGS]


def test_build_launch_custom_node(monkeypatch):
    monkeypatch.setenv("VT_NODE_BIN", "/opt/node20/bin/node")
    command, _ = build_mcp_launch("/opt/mcp/cli.js", MCP_ARGS)
    assert command == "/opt/node20/bin/node"


def test_build_launch_npx_fallback():
    command, args = build_mcp_launch("", MCP_ARGS)
    assert command == "npx"
    assert args == ["-y", "@playwright/mcp", *MCP_ARGS]
