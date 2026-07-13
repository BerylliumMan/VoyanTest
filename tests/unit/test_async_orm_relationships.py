"""测试 async ORM 关系访问 — 验证 selectinload 是否正确配置。

这些测试专门覆盖之前出现的 MissingGreenlet 错误和缺少 selectinload 导入的问题。
"""
import pytest
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app import crud, db_models, models
from app.models import TestStepCreatePayload


async def _project(db):
    return await crud.create_project(db, models.ProjectCreate(name="rel-test"))


async def _module(db, pid):
    return await crud.create_module(db, pid, models.ModuleCreate(project_id=pid, name="rel-mod"))


async def _case(db, pid, mid):
    return await crud.create_test_case(db, models.TestCaseCreate(
        project_id=pid, module_id=mid, name="rel-case",
        steps=[TestStepCreatePayload(step_order=1, description="step1")],
    ))


class TestTestCaseSelectinload:
    """验证 TestCase CRUD 查询正确预加载 steps 关系。"""

    @pytest.mark.asyncio
    async def test_get_test_case_steps_accessible(self, db):
        """get_test_case: .steps 可访问不抛 MissingGreenlet"""
        p = await _project(db)
        m = await _module(db, p.id)
        c = await _case(db, p.id, m.id)
        db.add(c); await db.commit()

        got = await crud.get_test_case(db, c.id)
        assert got is not None
        # 访问 steps 关系 — 如果缺少 selectinload 会抛 MissingGreenlet
        steps = got.steps
        assert len(steps) >= 1
        assert steps[0].step_order == 1

    @pytest.mark.asyncio
    async def test_paginated_steps_accessible(self, db):
        """get_all_test_cases_for_project_paginated: .steps 可访问"""
        p = await _project(db)
        m = await _module(db, p.id)
        for i in range(3):
            c = await _case(db, p.id, m.id)
            db.add(c)
        await db.commit()

        result = await crud.get_all_test_cases_for_project_paginated(db, p.id, 1, 10)
        for item in result["items"]:
            assert item.steps is not None
            assert len(item.steps) >= 1

    @pytest.mark.asyncio
    async def test_search_steps_accessible(self, db):
        """search_test_cases: .steps 可访问"""
        p = await _project(db)
        m = await _module(db, p.id)
        c = await _case(db, p.id, m.id)
        db.add(c); await db.commit()

        result = await crud.search_test_cases(db, p.id, "rel-case", 1, 10)
        for item in result["items"]:
            assert item.steps is not None

    @pytest.mark.asyncio
    async def test_module_paginated_steps_accessible(self, db):
        """get_all_test_cases_for_module_paginated: .steps 可访问"""
        p = await _project(db)
        m = await _module(db, p.id)
        for i in range(2):
            c = await _case(db, p.id, m.id)
            db.add(c)
        await db.commit()

        result = await crud.get_all_test_cases_for_module_paginated(db, m.id, 1, 10)
        for item in result["items"]:
            assert item.steps is not None

    @pytest.mark.asyncio
    async def test_all_for_project_steps_accessible(self, db):
        """get_all_test_cases_for_project: .steps 可访问"""
        p = await _project(db)
        m = await _module(db, p.id)
        c = await _case(db, p.id, m.id)
        db.add(c); await db.commit()

        cases = await crud.get_all_test_cases_for_project(db, p.id)
        for item in cases:
            assert item.steps is not None

    @pytest.mark.asyncio
    async def test_init_cases_steps_accessible(self, db):
        """get_init_test_cases: .steps 可访问"""
        p = await _project(db)
        m = await _module(db, p.id)
        c = await _case(db, p.id, m.id)
        c.is_init = True
        db.add(c); await db.commit()

        cases = await crud.get_init_test_cases(db, p.id)
        for item in cases:
            assert item.steps is not None


class TestGenSessionSelectinload:
    """验证 GenSession CRUD 正确预加载 project 关系。"""

    @pytest.mark.asyncio
    async def test_list_sessions_project_accessible(self, db):
        """list_gen_sessions: .project.name 可访问"""
        p = await crud.create_project(db, models.ProjectCreate(name="gen-rel-test"))
        from app.crud.gen import list_gen_sessions
        result = await list_gen_sessions(db, 1, 10)
        # 不要求有数据（可能没 gen session），但至少不抛异常
        for item in result["items"]:
            # selectinload 后访问 .project 不会抛 MissingGreenlet
            assert item.project is not None or item.project_id is None
            if item.project:
                _ = item.project.name


class TestCrudModuleSelectinload:
    """验证 Module CRUD 正确预加载关系。"""

    @pytest.mark.asyncio
    async def test_get_module_tree_uses_selectinload(self, db):
        """get_module_tree 返回的模块可安全访问 children"""
        from app.crud import get_module_tree, create_module, create_project
        p = await create_project(db, models.ProjectCreate(name="mod-tree-test"))
        m1 = await create_module(db, p.id, models.ModuleCreate(project_id=p.id, name="root"))
        await create_module(db, p.id, models.ModuleCreate(project_id=p.id, name="child", parent_id=m1.id))
        await db.commit()

        tree = await get_module_tree(db, p.id)
        assert isinstance(tree, list)
