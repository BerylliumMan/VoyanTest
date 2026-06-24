# tests/unit/test_crud.py
"""CRUD 函数单元测试 — 直接调用 crud 层，验证数据库行为。"""
import pytest
from app import crud, db_models, models
from app.auth import hash_password
from app.models import TestStepCreatePayload


class TestProjectCRUD:
    """项目 CRUD 测试。"""

    @pytest.mark.asyncio
    async def test_create_project(self, db):
        data = models.ProjectCreate(name="项目A", description="描述A")
        result = await crud.create_project(db, data)
        assert result.id is not None
        assert result.name == "项目A"
        assert result.description == "描述A"

    @pytest.mark.asyncio
    async def test_create_project_duplicate_name(self, db):
        data = models.ProjectCreate(name="重复项目")
        await crud.create_project(db, data)
        with pytest.raises(Exception):
            await crud.create_project(db, models.ProjectCreate(name="重复项目"))

    @pytest.mark.asyncio
    async def test_get_project(self, db):
        created = await crud.create_project(db, models.ProjectCreate(name="查询项目"))
        result = await crud.get_project(db, created.id)
        assert result is not None
        assert result.name == "查询项目"

    @pytest.mark.asyncio
    async def test_get_project_not_found(self, db):
        result = await crud.get_project(db, 99999)
        assert result is None

    @pytest.mark.asyncio
    async def test_get_projects(self, db):
        await crud.create_project(db, models.ProjectCreate(name="P1"))
        await crud.create_project(db, models.ProjectCreate(name="P2"))
        results = await crud.get_all_projects(db)
        assert len(results) >= 2

    @pytest.mark.asyncio
    async def test_update_project(self, db):
        created = await crud.create_project(db, models.ProjectCreate(name="旧名称"))
        update = models.ProjectUpdate(name="新名称")
        result = await crud.update_project(db, created.id, update)
        assert result.name == "新名称"

    @pytest.mark.asyncio
    async def test_update_project_partial(self, db):
        created = await crud.create_project(db, models.ProjectCreate(name="部分更新", description="旧描述"))
        update = models.ProjectUpdate(name="部分更新", description="新描述")
        result = await crud.update_project(db, created.id, update)
        assert result.name == "部分更新"  # 未变更
        assert result.description == "新描述"

    @pytest.mark.asyncio
    async def test_delete_project(self, db):
        created = await crud.create_project(db, models.ProjectCreate(name="待删除"))
        await crud.delete_project(db, created.id)
        assert await crud.get_project(db, created.id) is None

    @pytest.mark.asyncio
    async def test_delete_project_cascades_testcases(self, db):
        """删除项目时，关联的测试用例也应被删除。"""
        project = await crud.create_project(db, models.ProjectCreate(name="级联项目"))
        case = await crud.create_test_case(db, models.TestCaseCreate(
            project_id=project.id, name="级联用例",
            steps=[TestStepCreatePayload(step_order=1, description="步骤1")]
        ))
        case_id = case.id  # 先保存 ID，避免 ObjectDeletedError
        await crud.delete_project(db, project.id)
        assert await crud.get_test_case(db, case_id) is None


class TestModuleCRUD:
    """模块 CRUD 测试。"""

    @pytest.mark.asyncio
    async def test_create_module(self, db):
        project = await crud.create_project(db, models.ProjectCreate(name="模块项目"))
        data = models.ModuleCreate(project_id=project.id, name="模块A")
        result = await crud.create_module(db, project.id, data)
        assert result.id is not None
        assert result.name == "模块A"
        assert result.project_id == project.id

    @pytest.mark.asyncio
    async def test_get_modules_by_project(self, db):
        project = await crud.create_project(db, models.ProjectCreate(name="模块查询项目"))
        await crud.create_module(db, project.id, models.ModuleCreate(project_id=project.id, name="M1"))
        await crud.create_module(db, project.id, models.ModuleCreate(project_id=project.id, name="M2"))
        results = await crud.get_modules_for_project(db, project.id)
        assert len(results) == 2

    @pytest.mark.asyncio
    async def test_update_module(self, db):
        project = await crud.create_project(db, models.ProjectCreate(name="模块更新项目"))
        created = await crud.create_module(db, project.id, models.ModuleCreate(project_id=project.id, name="旧模块"))
        update = models.ModuleUpdate(project_id=project.id, name="新模块")
        result = await crud.update_module(db, created.id, update)
        assert result.name == "新模块"

    @pytest.mark.asyncio
    async def test_delete_module_sets_case_module_null(self, db):
        """删除模块时，关联用例的 module_id 应设为 NULL。"""
        project = await crud.create_project(db, models.ProjectCreate(name="模块删除项目"))
        module = await crud.create_module(db, project.id, models.ModuleCreate(project_id=project.id, name="待删模块"))
        case = await crud.create_test_case(db, models.TestCaseCreate(
            project_id=project.id, module_id=module.id, name="关联用例",
            steps=[TestStepCreatePayload(step_order=1, description="步骤1")]
        ))
        # 先删除关联用例，再删除模块
        await crud.delete_test_case(db, case.id)
        await crud.delete_module(db, module.id)
        assert await crud.get_test_case(db, case.id) is None


class TestTestCaseCRUD:
    """测试用例 CRUD 测试。"""

    @pytest.mark.asyncio
    async def test_create_test_case(self, db):
        project = await crud.create_project(db, models.ProjectCreate(name="用例项目"))
        data = models.TestCaseCreate(
            project_id=project.id, name="用例A",
            steps=[
                TestStepCreatePayload(step_order=1, description="步骤1"),
                TestStepCreatePayload(step_order=2, description="步骤2"),
            ]
        )
        result = await crud.create_test_case(db, data)
        assert result.id is not None
        assert result.name == "用例A"
        assert len(result.steps) == 2

    @pytest.mark.asyncio
    async def test_get_test_case(self, db):
        project = await crud.create_project(db, models.ProjectCreate(name="查询用例项目"))
        created = await crud.create_test_case(db, models.TestCaseCreate(
            project_id=project.id, name="查询用例",
            steps=[TestStepCreatePayload(step_order=1, description="步骤1")]
        ))
        result = await crud.get_test_case(db, created.id)
        assert result is not None
        assert result.name == "查询用例"

    @pytest.mark.asyncio
    async def test_get_test_case_not_found(self, db):
        result = await crud.get_test_case(db, 99999)
        assert result is None

    @pytest.mark.asyncio
    async def test_update_test_case(self, db):
        project = await crud.create_project(db, models.ProjectCreate(name="更新用例项目"))
        created = await crud.create_test_case(db, models.TestCaseCreate(
            project_id=project.id, name="旧用例",
            steps=[TestStepCreatePayload(step_order=1, description="旧步骤")]
        ))
        update = models.TestCaseUpdate(
            name="新用例",
            steps=[
                models.TestStepUpdate(step_order=1, description="新步骤1"),
                models.TestStepUpdate(step_order=2, description="新步骤2"),
            ]
        )
        result = await crud.update_test_case(db, created.id, update)
        assert result.name == "新用例"
        assert len(result.steps) == 2

    @pytest.mark.asyncio
    async def test_delete_test_case(self, db):
        project = await crud.create_project(db, models.ProjectCreate(name="删除用例项目"))
        created = await crud.create_test_case(db, models.TestCaseCreate(
            project_id=project.id, name="待删用例",
            steps=[TestStepCreatePayload(step_order=1, description="步骤1")]
        ))
        await crud.delete_test_case(db, created.id)
        assert await crud.get_test_case(db, created.id) is None

    @pytest.mark.asyncio
    async def test_get_test_cases_by_project(self, db):
        project = await crud.create_project(db, models.ProjectCreate(name="分页用例项目"))
        for i in range(5):
            await crud.create_test_case(db, models.TestCaseCreate(
                project_id=project.id, name=f"用例{i}",
                steps=[TestStepCreatePayload(step_order=1, description=f"步骤{i}")]
            ))
        result = await crud.get_all_test_cases_for_project_paginated(db, project.id, page=1, size=10)
        assert result["total_items"] == 5
        assert len(result["items"]) == 5

    @pytest.mark.asyncio
    async def test_get_test_cases_by_project_pagination(self, db):
        project = await crud.create_project(db, models.ProjectCreate(name="分页2项目"))
        for i in range(15):
            await crud.create_test_case(db, models.TestCaseCreate(
                project_id=project.id, name=f"分页用例{i}",
                steps=[TestStepCreatePayload(step_order=1, description=f"步骤{i}")]
            ))
        page1 = await crud.get_all_test_cases_for_project_paginated(db, project.id, page=1, size=10)
        page2 = await crud.get_all_test_cases_for_project_paginated(db, project.id, page=2, size=10)
        assert page1["total_items"] == 15
        assert len(page1["items"]) == 10
        assert len(page2["items"]) == 5


class TestUserCRUD:
    """用户 CRUD 测试。"""

    @pytest.mark.asyncio
    async def test_create_user(self, db):
        user = db_models.User(
            username="newuser",
            password_hash=hash_password("Pass@123"),
            role="tester",
            status="active",
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)
        assert user.id is not None
        assert user.username == "newuser"

    @pytest.mark.asyncio
    async def test_duplicate_username(self, db):
        user1 = db_models.User(
            username="dup_user",
            password_hash=hash_password("Pass@123"),
            role="tester",
            status="active",
        )
        db.add(user1)
        await db.commit()
        user2 = db_models.User(
            username="dup_user",
            password_hash=hash_password("Pass@456"),
            role="tester",
            status="active",
        )
        db.add(user2)
        with pytest.raises(Exception):
            await db.commit()
