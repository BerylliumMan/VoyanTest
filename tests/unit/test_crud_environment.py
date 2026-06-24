# tests/unit/test_crud_environment.py
"""环境 CRUD 函数单元测试 — 直接调用 crud 层，验证数据库行为。"""
import pytest
from app import crud, db_models, models


async def _create_project(db, name="环境测试项目", base_url="https://example.com"):
    return await crud.create_project(db, models.ProjectCreate(
        name=name, base_url=base_url, browser="chromium", headless=True,
    ))


def _make_env_create(name="测试环境", base_url="https://example.com",
                     browser="chromium", headless=True, cookies=None):
    return models.EnvironmentCreate(
        name=name, base_url=base_url, browser=browser, headless=headless,
        cookies=cookies or [],
    )


class TestGetEnvironments:

    @pytest.mark.asyncio
    async def test_returns_list(self, db):
        project = await _create_project(db)
        await crud.create_environment(db, project.id, _make_env_create(name="A", base_url="https://a.com"))
        await crud.create_environment(db, project.id, _make_env_create(name="B", base_url="https://b.com"))
        assert len(await crud.get_environments(db, project.id)) == 2

    @pytest.mark.asyncio
    async def test_ordered_by_created_at(self, db):
        project = await _create_project(db)
        await crud.create_environment(db, project.id, _make_env_create(name="A"))
        await crud.create_environment(db, project.id, _make_env_create(name="B"))
        result = await crud.get_environments(db, project.id)
        assert result[0].name == "A"
        assert result[1].name == "B"

    @pytest.mark.asyncio
    async def test_scoped_to_project(self, db):
        p1 = await _create_project(db, name="P-scope-1")
        p2 = await _create_project(db, name="P-scope-2")
        await crud.create_environment(db, p1.id, _make_env_create())
        assert await crud.get_environments(db, p2.id) == []

    @pytest.mark.asyncio
    async def test_empty(self, db):
        project = await _create_project(db)
        assert await crud.get_environments(db, project.id) == []


class TestGetEnvironment:

    @pytest.mark.asyncio
    async def test_found(self, db):
        project = await _create_project(db)
        env = await crud.create_environment(db, project.id, _make_env_create())
        result = await crud.get_environment(db, env.id)
        assert result is not None
        assert result.id == env.id
        assert result.name == "测试环境"

    @pytest.mark.asyncio
    async def test_not_found(self, db):
        assert await crud.get_environment(db, 99999) is None


class TestCreateEnvironment:

    @pytest.mark.asyncio
    async def test_first_is_default(self, db):
        project = await _create_project(db)
        env = await crud.create_environment(db, project.id, _make_env_create(name="第一个环境"))
        assert env.is_default is True
        assert env.name == "第一个环境"
        assert env.base_url == "https://example.com"
        assert env.browser == "chromium"
        assert env.headless is True
        assert env.cookies == []
        assert env.project_id == project.id
        assert env.id is not None

    @pytest.mark.asyncio
    async def test_second_not_default(self, db):
        project = await _create_project(db)
        env1 = await crud.create_environment(db, project.id, _make_env_create(name="第一个"))
        env2 = await crud.create_environment(db, project.id, _make_env_create(name="第二个"))
        assert env1.is_default is True
        assert env2.is_default is False

    @pytest.mark.asyncio
    async def test_first_syncs_to_project(self, db):
        project = await _create_project(db, name="同步项目")
        await crud.create_environment(
            db, project.id,
            _make_env_create(base_url="https://synced.com", browser="firefox", headless=False),
        )
        assert project.base_url == "https://synced.com"
        assert project.browser == "firefox"
        assert project.headless is False

    @pytest.mark.asyncio
    async def test_second_does_not_sync(self, db):
        project = await _create_project(db, name="非同步项目")
        await crud.create_environment(db, project.id, _make_env_create(name="env1", base_url="https://url1.com"))
        assert project.base_url == "https://url1.com"
        await crud.create_environment(db, project.id, _make_env_create(name="env2", base_url="https://url2.com"))
        assert project.base_url == "https://url1.com"


class TestUpdateEnvironment:

    @pytest.mark.asyncio
    async def test_found(self, db):
        project = await _create_project(db)
        env = await crud.create_environment(db, project.id, _make_env_create(name="旧名称"))
        result = await crud.update_environment(db, env.id, models.EnvironmentUpdate(name="新名称"))
        assert result is not None
        assert result.name == "新名称"

    @pytest.mark.asyncio
    async def test_not_found(self, db):
        assert await crud.update_environment(db, 99999, models.EnvironmentUpdate(name="x")) is None

    @pytest.mark.asyncio
    async def test_partial_update(self, db):
        project = await _create_project(db)
        env = await crud.create_environment(
            db, project.id,
            _make_env_create(name="原始名称", base_url="https://original.com", headless=False),
        )
        result = await crud.update_environment(db, env.id, models.EnvironmentUpdate(name="新名称"))
        assert result.name == "新名称"
        assert result.base_url == "https://original.com"
        assert result.headless is False

    @pytest.mark.asyncio
    async def test_default_triggers_sync(self, db):
        project = await _create_project(db, name="同步更新项目")
        env = await crud.create_environment(db, project.id, _make_env_create(base_url="https://old.com"))
        assert env.is_default is True
        await crud.update_environment(db, env.id, models.EnvironmentUpdate(base_url="https://updated.com"))
        assert project.base_url == "https://updated.com"

    @pytest.mark.asyncio
    async def test_non_default_no_sync(self, db):
        project = await _create_project(db, name="非默认更新项目")
        await crud.create_environment(db, project.id, _make_env_create(name="e1", base_url="https://e1.com"))
        env2 = await crud.create_environment(db, project.id, _make_env_create(name="e2", base_url="https://e2.com"))
        assert env2.is_default is False
        assert project.base_url == "https://e1.com"
        await crud.update_environment(db, env2.id, models.EnvironmentUpdate(base_url="https://changed.com"))
        assert project.base_url == "https://e1.com"


class TestDeleteEnvironment:

    @pytest.mark.asyncio
    async def test_found(self, db):
        project = await _create_project(db)
        env = await crud.create_environment(db, project.id, _make_env_create())
        assert await crud.delete_environment(db, env.id) == {"message": f"环境 {env.id} 已删除"}
        assert await crud.get_environment(db, env.id) is None

    @pytest.mark.asyncio
    async def test_not_found(self, db):
        assert await crud.delete_environment(db, 99999) is None

    @pytest.mark.asyncio
    async def test_default_promotes_next(self, db):
        project = await _create_project(db)
        env1 = await crud.create_environment(db, project.id, _make_env_create(name="默认环境"))
        env2 = await crud.create_environment(db, project.id, _make_env_create(name="环境2"))
        assert env1.is_default is True
        assert env2.is_default is False
        await crud.delete_environment(db, env1.id)
        assert env2.is_default is True

    @pytest.mark.asyncio
    async def test_default_promote_syncs_to_project(self, db):
        project = await _create_project(db, name="提升同步项目")
        await crud.create_environment(db, project.id, _make_env_create(name="默认环境", base_url="https://a.com"))
        env2 = await crud.create_environment(db, project.id, _make_env_create(name="环境2", base_url="https://b.com"))
        assert project.base_url == "https://a.com"
        await crud.delete_environment(db, env2.id)
        assert project.base_url == "https://a.com"
        await crud.create_environment(db, project.id, _make_env_create(name="环境3", base_url="https://c.com"))
        await crud.delete_environment(db, env2.id)

    @pytest.mark.asyncio
    async def test_non_default(self, db):
        project = await _create_project(db)
        env1 = await crud.create_environment(db, project.id, _make_env_create(name="默认环境"))
        env2 = await crud.create_environment(db, project.id, _make_env_create(name="非默认环境"))
        assert env1.is_default is True
        assert await crud.delete_environment(db, env2.id) is not None
        assert await crud.get_environment(db, env1.id).is_default is True

    @pytest.mark.asyncio
    async def test_last_env(self, db):
        project = await _create_project(db)
        env = await crud.create_environment(db, project.id, _make_env_create())
        await crud.delete_environment(db, env.id)
        assert await crud.get_environments(db, project.id) == []


class TestSetDefaultEnvironment:

    @pytest.mark.asyncio
    async def test_sets_default(self, db):
        project = await _create_project(db)
        env1 = await crud.create_environment(db, project.id, _make_env_create(name="e1"))
        env2 = await crud.create_environment(db, project.id, _make_env_create(name="e2"))
        assert env1.is_default is True
        assert env2.is_default is False
        result = await crud.set_default_environment(db, env2.id)
        assert result is not None
        assert result.is_default is True
        assert result.id == env2.id

    @pytest.mark.asyncio
    async def test_clears_other_defaults(self, db):
        project = await _create_project(db)
        env1 = await crud.create_environment(db, project.id, _make_env_create(name="e1"))
        env2 = await crud.create_environment(db, project.id, _make_env_create(name="e2"))
        assert env1.is_default is True
        await crud.set_default_environment(db, env2.id)
        assert env1.is_default is False

    @pytest.mark.asyncio
    async def test_not_found(self, db):
        assert await crud.set_default_environment(db, 99999) is None

    @pytest.mark.asyncio
    async def test_syncs_to_project(self, db):
        project = await _create_project(db, name="设置默认项目")
        await crud.create_environment(db, project.id, _make_env_create(name="e1", base_url="https://e1.com"))
        env2 = await crud.create_environment(db, project.id, _make_env_create(name="e2", base_url="https://e2.com"))
        assert project.base_url == "https://e1.com"
        await crud.set_default_environment(db, env2.id)
        assert project.base_url == "https://e2.com"


class TestEnsureDefaultEnvironment:

    @pytest.mark.asyncio
    async def test_returns_early_when_env_exists(self, db):
        project = await _create_project(db)
        await crud.create_environment(db, project.id, _make_env_create())
        await crud.ensure_default_environment(db, project.id)
        assert len(await crud.get_environments(db, project.id)) == 1

    @pytest.mark.asyncio
    async def test_creates_from_project(self, db):
        project = await _create_project(db, name="自动创建项目", base_url="https://autocreate.com")
        await crud.ensure_default_environment(db, project.id)
        envs = await crud.get_environments(db, project.id)
        assert len(envs) == 1
        assert envs[0].name == "default"
        assert envs[0].base_url == "https://autocreate.com"
        assert envs[0].browser == "chromium"
        assert envs[0].headless is True
        assert envs[0].is_default is True

    @pytest.mark.asyncio
    async def test_skips_when_project_has_no_base_url(self, db):
        project = await crud.create_project(db, models.ProjectCreate(name="无 BaseUrl 项目"))
        await crud.ensure_default_environment(db, project.id)
        assert await crud.get_environments(db, project.id) == []

    @pytest.mark.asyncio
    async def test_skips_when_project_missing(self, db):
        await crud.ensure_default_environment(db, 99999)


class TestSyncEnvToProject:

    @pytest.mark.asyncio
    async def test_no_op_when_project_missing(self, db):
        env = db_models.Environment(
            project_id=99999, name="tmp", base_url="https://x.com",
            browser="chromium", headless=True, is_default=True,
        )
        await crud._sync_env_to_project(db, 99999, env)
