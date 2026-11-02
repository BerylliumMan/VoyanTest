"""046：Git 拉取与差异测试（全部基于本地仓库，不联网）。"""

from pathlib import Path

import pytest
from dulwich import porcelain

from core.git_sync import (
    GitSyncError,
    RepoAuth,
    build_auth_url,
    clone_repository,
    head_commit,
    list_changed_files,
    mask_url,
    update_repository,
)

AUTHOR = b"tester <tester@example.invalid>"


def _commit(repo: Path, files: dict[str, str], message: str) -> str:
    for name, text in files.items():
        (repo / name).write_text(text, encoding="utf-8")
    porcelain.add(repo=str(repo), paths=list(files))
    return porcelain.commit(
        repo=str(repo), message=message.encode(), author=AUTHOR, committer=AUTHOR
    ).decode()


def _init_repo(path: Path) -> str:
    porcelain.init(str(path))
    return _commit(path, {"A.java": "@RestController\nclass A {}\n"}, "init")


def test_build_auth_url_injects_https_credentials_and_masks():
    auth = RepoAuth(kind="https", username="robot", secret="s3cr3t-token")
    url = build_auth_url("https://github.com/acme/demo.git", auth)
    assert url == "https://robot:s3cr3t-token@github.com/acme/demo.git"

    masked = mask_url(url)
    assert "s3cr3t-token" not in masked
    assert "robot" not in masked
    assert masked.startswith("https://***@github.com/acme/demo.git")


def test_build_auth_url_keeps_ssh_and_public_urls():
    ssh_auth = RepoAuth(kind="ssh", secret="-----BEGIN KEY-----")
    assert (
        build_auth_url("git@github.com:acme/demo.git", ssh_auth)
        == "git@github.com:acme/demo.git"
    )
    assert build_auth_url("https://github.com/acme/demo.git", None) == (
        "https://github.com/acme/demo.git"
    )


def test_build_auth_url_rejects_unknown_kind():
    with pytest.raises(GitSyncError):
        build_auth_url("https://example.com/x.git", RepoAuth(kind="ftp"))


def test_clone_local_repository_checks_out_files(tmp_path):
    src = tmp_path / "src"
    head = _init_repo(src)
    dst = tmp_path / "dst"

    result = clone_repository(str(src), str(dst), timeout=30)
    assert result.head_commit == head
    assert (dst / "A.java").is_file()
    assert head_commit(str(dst)) == head


def test_clone_missing_repository_raises_git_sync_error(tmp_path):
    with pytest.raises(GitSyncError):
        clone_repository(str(tmp_path / "nope"), str(tmp_path / "dst"), timeout=30)


def test_list_changed_files_reports_add_modify_delete(tmp_path):
    src = tmp_path / "src"
    c1 = _init_repo(src)
    c2 = _commit(
        src,
        {"A.java": "@RestController\nclass A { int x; }\n", "B.java": "class B {}\n"},
        "modify+add",
    )
    (src / "B.java").unlink()
    porcelain.add(repo=str(src), paths=["B.java"])
    c3 = porcelain.commit(
        repo=str(src), message=b"delete", author=AUTHOR, committer=AUTHOR
    ).decode()

    assert list_changed_files(str(src), c1, c2) == ["A.java", "B.java"]
    assert list_changed_files(str(src), c2, c3) == ["B.java"]
    assert list_changed_files(str(src), c1, c1) == []


def test_update_repository_pulls_new_commits(tmp_path):
    src = tmp_path / "src"
    _init_repo(src)
    dst = tmp_path / "dst"
    clone_repository(str(src), str(dst), timeout=30)

    new_head = _commit(src, {"C.java": "class C {}\n"}, "add C")
    old, new = update_repository(str(dst), str(src), timeout=30)

    assert new == new_head
    assert old != new
    assert (dst / "C.java").is_file()
    assert head_commit(str(dst)) == new_head


def test_head_commit_on_non_repo_returns_none(tmp_path):
    assert head_commit(str(tmp_path)) is None
