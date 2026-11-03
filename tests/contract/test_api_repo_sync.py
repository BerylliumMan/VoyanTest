"""046：仓库绑定 / 同步 / 变更计划 契约测试。"""

import json

import pytest


@pytest.fixture(autouse=True)
def _isolate_repo_root(tmp_path, monkeypatch):
    """仓库工作目录隔离到临时目录，避免用例之间复用旧克隆。"""
    import app.services.api_sync as api_sync

    monkeypatch.setattr(api_sync, "REPO_ROOT", tmp_path / "repos")


@pytest.fixture(autouse=True)
def _mark_db_ready(db):
    """契约 conftest 用普通 sessionmaker 覆盖 AsyncSessionLocal；恢复 is_ready 语义。"""
    import app.database as db_mod

    previous = db_mod.AsyncSessionLocal
    wrapper = db_mod._LazySessionMaker()
    wrapper.configure(previous)
    db_mod.AsyncSessionLocal = wrapper
    yield
    db_mod.AsyncSessionLocal = previous


def _init_repo(path):
    from dulwich import porcelain

    path.mkdir(parents=True, exist_ok=True)
    porcelain.init(str(path))
    java = path / "src/main/java/com/demo/UserController.java"
    java.parent.mkdir(parents=True, exist_ok=True)
    java.write_text(
        '@RestController\n@RequestMapping("/api/users")\n'
        "public class UserController {\n"
        '  @GetMapping("/{id}")\n'
        '  public Object get(@PathVariable("id") Long id) { return null; }\n'
        "}\n",
        encoding="utf-8",
    )
    porcelain.add(repo=str(path), paths=["src/main/java/com/demo/UserController.java"])
    porcelain.commit(
        repo=str(path),
        message=b"init",
        author=b"tester <t@example.invalid>",
        committer=b"tester <t@example.invalid>",
    )


def _bind(client, cookies, project_id: int, payload: dict | None = None):
    body = {
        "project_id": project_id,
        "repo_url": "https://git.example.com/acme/demo.git",
        "branch": "main",
        "auth_type": "https_token",
        "auth_secret": "s3cr3t-token",
        "sync_mode": "manual",
    }
    body.update(payload or {})
    return client.post("/api/api-test/repos", json=body, cookies=cookies)


def test_bind_repo_masks_secret(client, admin_cookies, sample_project):
    project_id = sample_project["id"]
    resp = _bind(client, admin_cookies, project_id)
    assert resp.status_code == 201, resp.text
    binding = resp.json()["binding"]
    assert binding["auth_secret"] == "******"
    assert binding["has_secret"] is True
    assert "s3cr3t-token" not in json.dumps(binding)
    assert binding["repo_url"] == "https://git.example.com/acme/demo.git"


def test_bind_is_idempotent(client, admin_cookies, sample_project):
    project_id = sample_project["id"]
    first = _bind(client, admin_cookies, project_id)
    assert first.json()["created"] is True

    second = _bind(client, admin_cookies, project_id, {"branch": "develop"})
    assert second.json()["created"] is False
    assert second.json()["binding"]["branch"] == "develop"

    got = client.get(f"/api/api-test/repos/{project_id}", cookies=admin_cookies)
    assert got.json()["bound"] is True
    assert got.json()["binding"]["branch"] == "develop"


def test_repo_url_with_userinfo_is_split(client, admin_cookies, sample_project):
    """URL 里带 user:pass 时凭据落入密文，地址本身不留明文。"""
    resp = _bind(
        client,
        admin_cookies,
        sample_project["id"],
        {"repo_url": "https://robot:tok-123@git.example.com/acme/demo.git", "auth_secret": None},
    )
    binding = resp.json()["binding"]
    assert binding["repo_url"] == "https://git.example.com/acme/demo.git"
    assert "tok-123" not in json.dumps(binding)
    assert binding["has_secret"] is True


def test_unbind_repo(client, admin_cookies, sample_project):
    project_id = sample_project["id"]
    _bind(client, admin_cookies, project_id)
    resp = client.delete(f"/api/api-test/repos/{project_id}", cookies=admin_cookies)
    assert resp.status_code == 200
    got = client.get(f"/api/api-test/repos/{project_id}", cookies=admin_cookies)
    assert got.json()["bound"] is False


def test_manual_sync_local_repo(client, admin_cookies, sample_project, tmp_path):
    project_id = sample_project["id"]
    repo = tmp_path / "origin"
    _init_repo(repo)

    _bind(client, admin_cookies, project_id, {"repo_url": str(repo), "branch": "", "auth_secret": None})
    resp = client.post(
        f"/api/api-test/repos/{project_id}/sync?wait=true", cookies=admin_cookies
    )
    assert resp.status_code == 200, resp.text

    records = client.get(
        f"/api/api-test/repos/{project_id}/syncs", cookies=admin_cookies
    ).json()["items"]
    assert records and records[0]["status"] == "success"
    assert records[0]["added"] == 1

    definitions = client.get(
        f"/api/api-test/definitions?project_id={project_id}", cookies=admin_cookies
    ).json()["items"]
    target = next(d for d in definitions if d["path"] == "/api/users/{id}")
    assert target["extractor"] == "spring_source"
    assert target["source_file"] == "src/main/java/com/demo/UserController.java"
    assert target["confidence"] == "high"


def test_test_connection_reports_failure(client, admin_cookies, sample_project, tmp_path):
    project_id = sample_project["id"]
    _bind(
        client,
        admin_cookies,
        project_id,
        {"repo_url": str(tmp_path / "missing"), "auth_secret": None},
    )
    resp = client.post(f"/api/api-test/repos/{project_id}/test", cookies=admin_cookies)
    assert resp.status_code == 200
    assert resp.json()["ok"] is False


def test_change_plans_empty_list(client, admin_cookies, sample_project):
    resp = client.get(
        f"/api/api-test/change-plans?project_id={sample_project['id']}",
        cookies=admin_cookies,
    )
    assert resp.status_code == 200
    assert resp.json()["items"] == []


def test_missing_binding_returns_unbound(client, admin_cookies, sample_project):
    resp = client.get(
        f"/api/api-test/repos/{sample_project['id']}", cookies=admin_cookies
    )
    assert resp.status_code == 200
    assert resp.json() == {"bound": False, "binding": None}


def test_manual_override_toggle_and_sync_protection(client, admin_cookies, sample_project, tmp_path):
    """人工修正标记可通过 API 设置，并在同步后保持。"""
    project_id = sample_project["id"]
    repo = tmp_path / "origin"
    _init_repo(repo)
    _bind(client, admin_cookies, project_id, {"repo_url": str(repo), "branch": "", "auth_secret": None})
    client.post(f"/api/api-test/repos/{project_id}/sync?wait=true", cookies=admin_cookies)

    defs = client.get(
        f"/api/api-test/definitions?project_id={project_id}", cookies=admin_cookies
    ).json()["items"]
    target = next(d for d in defs if d["path"] == "/api/users/{id}")

    put = client.put(
        f"/api/api-test/definitions/{target['id']}",
        json={"manual_override": True},
        cookies=admin_cookies,
    )
    assert put.status_code == 200
    assert put.json()["manual_override"] is True

    # 再次同步（无新提交 → no_change），标记仍保留
    client.post(f"/api/api-test/repos/{project_id}/sync?wait=true", cookies=admin_cookies)
    defs2 = client.get(
        f"/api/api-test/definitions?project_id={project_id}", cookies=admin_cookies
    ).json()["items"]
    again = next(d for d in defs2 if d["id"] == target["id"])
    assert again["manual_override"] is True


def test_binding_advanced_fields_and_cancel(client, admin_cookies, sample_project):
    project_id = sample_project["id"]
    resp = _bind(
        client,
        admin_cookies,
        project_id,
        {"confirm_window_minutes": 30, "full_sync_every": 5},
    )
    binding = resp.json()["binding"]
    assert binding["confirm_window_minutes"] == 30
    assert binding["full_sync_every"] == 5

    # 无运行中同步时取消返回 requested=false
    cancel = client.post(
        f"/api/api-test/repos/{project_id}/sync/cancel", cookies=admin_cookies
    )
    assert cancel.status_code == 200
    assert cancel.json()["requested"] is False


def test_change_plans_status_filter(client, admin_cookies, sample_project):
    resp = client.get(
        f"/api/api-test/change-plans?project_id={sample_project['id']}&status=pending",
        cookies=admin_cookies,
    )
    assert resp.status_code == 200
    assert resp.json()["items"] == []


def test_sync_check_auto_generates_cases(client, admin_cookies, sample_project, tmp_path):
    """047：手动「同步并检查变更」会给无用例的新接口自动补用例并建计划。"""
    project_id = sample_project["id"]
    repo = tmp_path / "origin"
    _init_repo(repo)
    _bind(client, admin_cookies, project_id, {"repo_url": str(repo), "branch": "", "auth_secret": None})

    # 首次同步建立基线（首次克隆不算变更）
    first = client.post(
        f"/api/api-test/repos/{project_id}/sync?wait=true", cookies=admin_cookies
    )
    assert first.status_code == 200, first.text

    # 源仓库新增一个接口（原接口无用例 → 应被自动补测）
    from dulwich import porcelain

    ctrl = repo / "src/main/java/com/demo/UserController.java"
    ctrl.write_text(
        ctrl.read_text().replace(
            "  public Object get(@PathVariable(\"id\") Long id) { return null; }",
            "  public Object get(@PathVariable(\"id\") Long id) { return null; }\n"
            '  @PostMapping("/add")\n'
            "  public Object add() { return null; }",
        )
    )
    porcelain.add(repo=str(repo), paths=["src/main/java/com/demo/UserController.java"])
    porcelain.commit(
        repo=str(repo),
        message=b"add endpoint",
        author=b"tester <t@example.invalid>",
        committer=b"tester <t@example.invalid>",
    )

    resp = client.post(
        f"/api/api-test/repos/{project_id}/sync?wait=true&check=true",
        cookies=admin_cookies,
    )
    assert resp.status_code == 200, resp.text

    plans = client.get(
        f"/api/api-test/change-plans?project_id={project_id}", cookies=admin_cookies
    ).json()["items"]
    assert plans, "应当生成变更计划"
    plan = plans[0]
    assert plan["case_count"] >= 1
    affected = plan["affected"]
    assert affected and affected[0]["case_ids"]
