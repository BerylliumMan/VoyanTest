"""Unit tests for voyan_cli.py CLI runner — argument parsing, exit codes, error handling."""

import asyncio
import os
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
CLI_SCRIPT = str(PROJECT_ROOT / "voyan_cli.py")
CWD = str(PROJECT_ROOT)


def _run_cli_sync(*args: str) -> subprocess.CompletedProcess:
    """Run the CLI as a subprocess with given args (sync helper).

    注意：conftest.py 全局设置了 DATABASE_URL=test_platform.db，
    子进程会继承它。这里还原为项目默认的 uitest.db，确保 CLI
    读写的是真实数据库而非测试数据库。
    """
    env = os.environ.copy()
    env["DATABASE_URL"] = "sqlite+aiosqlite:///./uitest.db"
    return subprocess.run(
        ["python3", CLI_SCRIPT, *args],
        capture_output=True,
        text=True,
        cwd=CWD,
        timeout=30,
        env=env,
    )


async def run_cli(*args: str) -> subprocess.CompletedProcess:
    """Async wrapper around the sync subprocess call."""
    return await asyncio.to_thread(_run_cli_sync, *args)


class TestHelpOutput:
    """验证 --help 输出包含预期的子命令和参数。"""

    @pytest.mark.asyncio
    async def test_top_level_help(self):
        """运行 --help 应显示包含 'run' 和 'list-projects' 的用法信息。"""
        result = await run_cli("--help")
        assert result.returncode == 0, f"expected exit 0, got {result.returncode}"
        assert "run" in result.stdout, f"'run' not in stdout:\n{result.stdout}"
        assert "list-projects" in result.stdout, f"'list-projects' not in stdout:\n{result.stdout}"

    @pytest.mark.asyncio
    async def test_run_help(self):
        """运行 'run --help' 应显示 --project-id 和 --case-ids 参数。"""
        result = await run_cli("run", "--help")
        assert result.returncode == 0, f"expected exit 0, got {result.returncode}"
        assert "--project-id" in result.stdout, f"'--project-id' not in stdout:\n{result.stdout}"
        assert "--case-ids" in result.stdout, f"'--case-ids' not in stdout:\n{result.stdout}"

    @pytest.mark.asyncio
    async def test_run_single_help(self):
        """运行 'run-single --help' 应显示 --case-id 参数。"""
        result = await run_cli("run-single", "--help")
        assert result.returncode == 0, f"expected exit 0, got {result.returncode}"
        assert "--case-id" in result.stdout, f"'--case-id' not in stdout:\n{result.stdout}"


class TestErrorHandling:
    """验证缺少参数、非法值等情况下的退出码和错误信息。"""

    @pytest.mark.asyncio
    async def test_no_subcommand(self):
        """不带任何子命令运行时应打印帮助信息并以退出码 1 退出。"""
        result = await run_cli()
        assert result.returncode == 1, f"expected exit 1, got {result.returncode}"
        combined = (result.stdout + result.stderr).lower()
        assert "usage" in combined or "voyan" in combined, (
            f"expected usage/help text, got stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )

    @pytest.mark.asyncio
    async def test_missing_required_arg(self):
        """运行 'run' 缺少 --project-id 时应失败并提示缺少必选参数。"""
        result = await run_cli("run")
        assert result.returncode != 0, f"expected non-zero exit, got {result.returncode}"
        assert (
            "--project-id" in result.stderr
            or "required" in result.stderr.lower()
            or "error" in result.stderr.lower()
        ), f"expected error about --project-id, got stderr:\n{result.stderr}"

    @pytest.mark.asyncio
    async def test_nonexistent_project(self):
        """运行 'run --project-id 99999' 对不存在的项目应返回退出码 2 并提示'未找到'。"""
        result = await run_cli("run", "--project-id", "99999")
        assert result.returncode == 2, (
            f"expected exit 2 (not found), got {result.returncode}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
        assert "未找到" in result.stderr, (
            f"expected '未找到' in stderr, got:\n{result.stderr}"
        )

    @pytest.mark.asyncio
    async def test_list_cases_requires_project_id(self):
        """运行 'list-cases' 缺少 --project-id 时应失败并提示缺少必选参数。"""
        result = await run_cli("list-cases")
        assert result.returncode != 0, f"expected non-zero exit, got {result.returncode}"
        assert (
            "--project-id" in result.stderr
            or "required" in result.stderr.lower()
            or "error" in result.stderr.lower()
        ), f"expected error about --project-id, got stderr:\n{result.stderr}"


class TestListCommands:
    """验证 list 类命令的正常执行（需要数据库连接）。"""

    @pytest.mark.asyncio
    async def test_list_projects_runs(self):
        """运行 'list-projects' 应正常退出（退出码 0）。"""
        result = await run_cli("list-projects")
        assert result.returncode == 0, (
            f"expected exit 0, got {result.returncode}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
