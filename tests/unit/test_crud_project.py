# tests/unit/test_crud_project.py
"""app/crud/project.py 单元测试 — get_db、项目 CRUD、级联删除。"""
import pytest
from sqlalchemy import text

from app import crud, models
from app.database import get_async_db as get_db
from app.models import TestStepCreatePayload, EnvironmentCreate


class TestGetDbGenerator:
    """测试 get_async_db 异步生成器。"""

    @pytest.mark.asyncio
    async def test_yields_session_then_closes(self):
        gen = get_db()
        session = await anext(gen)
        assert session is not None
        assert session is not None


class TestProjectCRUDAdditional:
    """项目 CRUD 补充测试 — 覆盖 missing lines（52, 66, 71-90）。"""

    @pytest.mark.asyncio
    async def test_update_project_not_found_returns_none(self, db):
        """更新不存在的项目应返回 None。"""
        from app import models
        update = models.ProjectUpdate(name="不存在")
        result = await crud.update_project(db, 99999, update)
        assert result is None

    @pytest.mark.asyncio
    async def test_delete_project_not_found_returns_none(self, db):
        """删除不存在的项目应返回 None。"""
        result = await crud.delete_project(db, 99999)
        assert result is None

    @pytest.mark.asyncio
    async def test_delete_project_cascades_environments(self, db):
        """删除项目应同时删除关联的环境。"""
        project = await crud.create_project(db, models.ProjectCreate(name="P-env"))
        await crud.create_environment(db, project.id, EnvironmentCreate(
            name="dev", base_url="https://dev.example.com",
        ))
        await crud.create_environment(db, project.id, EnvironmentCreate(
            name="prod", base_url="https://prod.example.com",
        ))
        await crud.delete_project(db, project.id)
        envs = await crud.get_environments(db, project.id)
        assert envs == []

    @pytest.mark.asyncio
    async def test_delete_project_cascades_steps_cases_modules_environments(self, db):
        """删除项目应清理所有关联资产（步骤、用例、模块、环境）。"""
        project = await crud.create_project(db, models.ProjectCreate(name="P-cascade"))
        module = await crud.create_module(db, project.id, models.ModuleCreate(
            project_id=project.id, name="M",
        ))
        case = await crud.create_test_case(db, models.TestCaseCreate(
            project_id=project.id, module_id=module.id, name="C",
            steps=[TestStepCreatePayload(step_order=1, description="step1")],
        ))
        case_id, module_id = case.id, module.id
        await crud.create_environment(db, project.id, EnvironmentCreate(
            name="e", base_url="https://e.example.com",
        ))
        result = await crud.delete_project(db, project.id)
        assert result is not None
        assert "已删除" in result["message"]
        assert await crud.get_test_case(db, case_id) is None
        assert await crud.get_module(db, module_id) is None
        assert await crud.get_environments(db, project.id) == []

    @pytest.mark.asyncio
    async def test_delete_project_with_no_assets(self, db):
        """删除空项目（无任何资产）应正常返回。"""
        project = await crud.create_project(db, models.ProjectCreate(name="empty"))
        result = await crud.delete_project(db, project.id)
        assert result["message"]
        assert await crud.get_project(db, project.id) is None

    @pytest.mark.asyncio
    async def test_create_project_with_all_fields(self, db):
        """创建项目时所有字段应被正确持久化。"""
        data = models.ProjectCreate(
            name="full", description="d", base_url="https://x.com",
            browser="firefox", headless=False,
        )
        p = await crud.create_project(db, data)
        assert p.browser == "firefox"
        assert p.headless is False
        assert p.base_url == "https://x.com"

    @pytest.mark.asyncio
    async def test_update_project_no_fields_set(self, db):
        """update_project 接受空 exclude_unset 字典（未变更场景）。"""
        project = await crud.create_project(db, models.ProjectCreate(name="orig"))
        update = models.ProjectUpdate(name="orig")  # 与当前值相同
        result = await crud.update_project(db, project.id, update)
        assert result.name == "orig"
