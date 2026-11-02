"""046：纯 Python Git 同步（dulwich，HTTPS token / SSH 私钥）。

生产容器没有 git CLI，因此这里不调用外部 git 进程。
"""

from __future__ import annotations

import io
import logging
import os
import tempfile
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

logger = logging.getLogger(__name__)

AUTH_HTTPS = "https"
AUTH_SSH = "ssh"
AUTH_KINDS = (AUTH_HTTPS, AUTH_SSH)


class GitSyncError(RuntimeError):
    """拉取 / 更新仓库失败。"""


@dataclass
class RepoAuth:
    """仓库凭据（secret 为 HTTPS token 或 SSH 私钥内容）。"""

    kind: str = AUTH_HTTPS
    username: str | None = None
    secret: str | None = None
    passphrase: str | None = None


@dataclass
class CloneResult:
    repo_dir: str
    head_commit: str | None
    branch: str | None


def build_auth_url(repo_url: str, auth: RepoAuth | None) -> str:
    """把 HTTPS 凭据注入 URL；SSH 与非 HTTP 地址保持原样。

    注意：带凭据的 URL 只用于传输，日志必须使用 `mask_url`。
    """
    if auth is None:
        return repo_url
    kind = (auth.kind or "").strip().lower()
    if kind == AUTH_SSH:
        return repo_url
    if kind != AUTH_HTTPS:
        raise GitSyncError(f"不支持的凭据类型：{auth.kind}（可选：https、ssh）")
    if not auth.username and not auth.secret:
        return repo_url

    parts = urlsplit(repo_url)
    if parts.scheme not in ("http", "https"):
        return repo_url
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    userinfo = quote(auth.username or "git", safe="")
    if auth.secret:
        userinfo = f"{userinfo}:{quote(auth.secret, safe='')}"
    return urlunsplit((parts.scheme, f"{userinfo}@{host}", parts.path, parts.query, parts.fragment))


def mask_url(url: str) -> str:
    """把 URL 中的 userinfo 打码，用于日志与接口返回。"""
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    if "@" not in parts.netloc:
        return url
    host = parts.netloc.rsplit("@", 1)[1]
    return urlunsplit((parts.scheme, f"***@{host}", parts.path, parts.query, parts.fragment))


def head_commit(repo_dir: str) -> str | None:
    """读取当前 HEAD；目录不是仓库或还没有提交时返回 None。"""
    try:
        from dulwich.repo import Repo

        with Repo(repo_dir) as repo:
            return repo.head().decode()
    except Exception:
        return None


def list_changed_files(repo_dir: str, old_commit: str, new_commit: str) -> list[str]:
    """列出两个提交之间新增/修改/删除的文件（相对仓库根目录）。"""
    if not old_commit or not new_commit or old_commit == new_commit:
        return []
    from dulwich.diff_tree import tree_changes
    from dulwich.repo import Repo

    with Repo(repo_dir) as repo:
        try:
            old = repo[old_commit.encode()]
            new = repo[new_commit.encode()]
        except KeyError as exc:
            raise GitSyncError(f"提交不存在：{exc}") from exc
        if old.tree == new.tree:
            return []
        files: set[str] = set()
        for change in tree_changes(repo.object_store, old.tree, new.tree):
            path = change.new.path if change.new is not None else change.old.path
            if path:
                files.add(path.decode("utf-8", "replace"))
        return sorted(files)


def clone_repository(
    repo_url: str,
    target_dir: str,
    *,
    branch: str | None = None,
    auth: RepoAuth | None = None,
    timeout: int = 120,
    depth: int | None = 1,
) -> CloneResult:
    """浅克隆仓库到 target_dir。失败抛 GitSyncError。"""
    target = Path(target_dir)
    if target.exists() and any(target.iterdir()):
        raise GitSyncError(f"目标目录非空：{target_dir}")

    key_file: str | None = None

    def _do_clone():
        nonlocal key_file
        from dulwich import porcelain

        kwargs: dict = {
            "source": build_auth_url(repo_url, auth),
            "target": str(target),
            "checkout": True,
            "errstream": io.BytesIO(),
        }
        if auth is not None and auth.kind == AUTH_SSH:
            _ensure_ssh_vendor()
            kwargs["ssh_command"] = _SSH_COMMAND
        if depth and depth > 0:
            kwargs["depth"] = depth
        if branch:
            kwargs["branch"] = branch.encode()
        if auth is not None:
            if auth.kind == AUTH_SSH and auth.secret:
                key_file = _write_private_key(auth.secret)
                kwargs["key_filename"] = key_file
            elif auth.kind == AUTH_HTTPS:
                if auth.username:
                    kwargs["username"] = auth.username
                if auth.secret:
                    kwargs["password"] = auth.secret
        porcelain.clone(**kwargs)

    try:
        _run_with_timeout(_do_clone, timeout, "克隆仓库")
    finally:
        if key_file:
            _unlink_quiet(key_file)

    return CloneResult(
        repo_dir=str(target),
        head_commit=head_commit(str(target)),
        branch=branch,
    )


def update_repository(
    repo_dir: str,
    repo_url: str,
    *,
    branch: str | None = None,
    auth: RepoAuth | None = None,
    timeout: int = 120,
) -> tuple[str | None, str | None]:
    """拉取远端更新并快进工作区，返回 (旧 HEAD, 新 HEAD)。"""
    old = head_commit(repo_dir)
    key_file: str | None = None

    def _do_update():
        nonlocal key_file
        from dulwich import porcelain

        if branch:
            _ensure_branch(repo_dir, branch)
        url = build_auth_url(repo_url, auth)
        extra: dict = {}
        if auth is not None and auth.kind == AUTH_SSH:
            _ensure_ssh_vendor()
            extra["ssh_command"] = _SSH_COMMAND
        if auth is not None:
            if auth.kind == AUTH_SSH and auth.secret:
                key_file = _write_private_key(auth.secret)
                extra["key_filename"] = key_file
            elif auth.kind == AUTH_HTTPS:
                if auth.username:
                    extra["username"] = auth.username
                if auth.secret:
                    extra["password"] = auth.secret
        buf = io.BytesIO()
        porcelain.fetch(repo=repo_dir, remote_location=url, errstream=buf, **extra)
        porcelain.pull(
            repo_dir,
            url,
            fast_forward=True,
            errstream=buf,
            **extra,
        )

    try:
        _run_with_timeout(_do_update, timeout, "更新仓库")
    finally:
        if key_file:
            _unlink_quiet(key_file)

    return old, head_commit(repo_dir)


def ls_remote(
    repo_url: str,
    *,
    auth: RepoAuth | None = None,
    timeout: int = 30,
) -> dict[str, str]:
    """列出远端引用（连通性测试用），返回 {ref: sha}。"""
    key_file: str | None = None

    def _do():
        nonlocal key_file
        from dulwich import porcelain

        kwargs: dict = {}
        if auth is not None and auth.kind == AUTH_SSH:
            _ensure_ssh_vendor()
            kwargs["ssh_command"] = _SSH_COMMAND
        if auth is not None:
            if auth.kind == AUTH_SSH and auth.secret:
                key_file = _write_private_key(auth.secret)
                kwargs["key_filename"] = key_file
            elif auth.kind == AUTH_HTTPS:
                if auth.username:
                    kwargs["username"] = auth.username
                if auth.secret:
                    kwargs["password"] = auth.secret
        refs = porcelain.ls_remote(build_auth_url(repo_url, auth), **kwargs)
        return {k.decode(): v.decode() for k, v in refs.items()}

    try:
        return _run_with_timeout(_do, timeout, "连接仓库")
    finally:
        if key_file:
            _unlink_quiet(key_file)


def _ensure_branch(repo_dir: str, branch: str) -> None:
    """切换到绑定的分支；当前分支不同且切换失败时报错，避免拉错分支。"""
    from dulwich import porcelain

    try:
        current = porcelain.active_branch(repo_dir).decode()
    except Exception:
        return
    if current == branch:
        return
    try:
        porcelain.checkout(repo_dir, target=branch)
    except Exception as exc:
        raise GitSyncError(f"无法切换到分支 {branch}：{exc}") from exc


# SSH 子进程参数：非交互、首次接受 host key、连接超时（容器内无 known_hosts）
_SSH_COMMAND = (
    "ssh -o StrictHostKeyChecking=accept-new -o BatchMode=yes "
    "-o ConnectTimeout=20"
)

_SSH_VENDOR_READY: bool | None = None


def _ensure_ssh_vendor() -> None:
    """优先使用 paramiko 做 SSH；dulwich 新版本已移除该 vendor 时退回系统 ssh。

    镜像内置 openssh-client，因此两种路径都可工作。
    """
    global _SSH_VENDOR_READY
    if _SSH_VENDOR_READY is not None:
        return
    try:
        from dulwich import client as dulwich_client
        from dulwich.contrib.paramiko_vendor import ParamikoSSHVendor

        if dulwich_client.get_ssh_vendor is not ParamikoSSHVendor:
            dulwich_client.get_ssh_vendor = ParamikoSSHVendor
        _SSH_VENDOR_READY = True
    except Exception:  # pragma: no cover - 依赖缺失时退回默认
        _SSH_VENDOR_READY = False
        logger.debug("paramiko SSH vendor 不可用，SSH 传输使用系统 ssh")


def _write_private_key(secret: str) -> str:
    """把 SSH 私钥写入仅当前用户可读的临时文件。"""
    fd, path = tempfile.mkstemp(prefix="api-sync-key-", suffix=".pem")
    try:
        os.write(fd, secret.encode("utf-8"))
    finally:
        os.close(fd)
    os.chmod(path, 0o600)
    return path


def _unlink_quiet(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def _run_with_timeout(fn, timeout: int, what: str):
    """给阻塞的 Git 操作加超时（dulwich 本身没有超时参数）。"""
    if not timeout or timeout <= 0:
        try:
            return fn()
        except GitSyncError:
            raise
        except Exception as exc:
            raise GitSyncError(f"{what}失败：{exc}") from exc

    executor = ThreadPoolExecutor(max_workers=1)
    future = executor.submit(fn)
    try:
        return future.result(timeout=timeout)
    except FuturesTimeoutError as exc:
        raise GitSyncError(f"{what}超时（{timeout} 秒）") from exc
    except GitSyncError:
        raise
    except Exception as exc:
        raise GitSyncError(f"{what}失败：{exc}") from exc
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
