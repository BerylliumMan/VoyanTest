# tests/unit/test_crud_module.py
"""app/crud/module.py 单元测试 — 模块 CRUD、树形结构、循环引用校验、级联删除。"""
import pytest

from app import crud, models
from app.models import TestStepCreatePayload


async def _make_project(db, name="P"):
    return crud.create_project(db, models.ProjectCreate(name=name))


async def _make_module(db, project_id, name, parent_id=None, description=None):
    return crud.create_module(
        db, project_id,
        models.ModuleCreate(
            project_id=project_id, name=name,
            description=description, parent_id=parent_id,
        ),
    )


class TestModuleCRUD:
    """基础模块 CRUD 覆盖（line 89 路径：update 中无字段可改）。"""

    @pytest.mark.asyncio
    async def test_create_module_with_parent(self, db):
        project = await _make_project(db)
        parent = await _make_module(db, project.id, "parent")
        child = await _make_module(db, project.id, "child", parent_id=parent.id)
        assert child.parent_id == parent.id

    @pytest.mark.asyncio
    async def test_get_module_not_found(self, db):
        assert await crud.get_module(db, 99999) is None

    @pytest.mark.asyncio
    async def test_get_modules_for_project_empty(self, db):
        project = await _make_project(db)
        assert await crud.get_modules_for_project(db, project.id) == []

    @pytest.mark.asyncio
    async def test_get_modules_for_project_sorted_by_name(self, db):
        project = await _make_project(db)
        await _make_module(db, project.id, "zebra")
        await _make_module(db, project.id, "alpha")
        await _make_module(db, project.id, "monkey")
        mods = await crud.get_modules_for_project(db, project.id)
        assert [m.name for m in mods] == ["alpha", "monkey", "zebra"]

    @pytest.mark.asyncio
    async def test_update_module_not_found_returns_none(self, db):
        result = await crud.update_module(
            db, 99999, models.ModuleUpdate(name="x"),
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_update_module_no_fields_keeps_existing(self, db):
        """model_dump(exclude_unset=True) 为空 → 不修改。"""
        project = await _make_project(db)
        m = await _make_module(db, project.id, "orig", description="d")
        result = await crud.update_module(
            db, m.id, models.ModuleUpdate(project_id=project.id),
        )
        assert result.name == "orig"
        assert result.description == "d"

    @pytest.mark.asyncio
    async def test_update_module_changes_description(self, db):
        project = await _make_project(db)
        m = await _make_module(db, project.id, "n", description="old")
        result = await crud.update_module(
            db, m.id, models.ModuleUpdate(
                project_id=project.id, description="new",
            ),
        )
        assert result.description == "new"

    @pytest.mark.asyncio
    async def test_update_module_changes_parent(self, db):
        project = await _make_project(db)
        p1 = await _make_module(db, project.id, "p1")
        p2 = await _make_module(db, project.id, "p2")
        child = await _make_module(db, project.id, "c", parent_id=p1.id)
        result = await crud.update_module(
            db, child.id, models.ModuleUpdate(
                project_id=project.id, parent_id=p2.id,
            ),
        )
        assert result.parent_id == p2.id

    @pytest.mark.asyncio
    async def test_delete_module_not_found_returns_none(self, db):
        assert await crud.delete_module(db, 99999) is None

    @pytest.mark.asyncio
    async def test_delete_module_with_no_cases_returns_true(self, db):
        project = await _make_project(db)
        m = await _make_module(db, project.id, "empty")
        assert await crud.delete_module(db, m.id) is True
        assert await crud.get_module(db, m.id) is None


class TestModuleTree:
    """覆盖 get_module_tree（line 38-62）。"""

    @pytest.mark.asyncio
    async def test_empty_tree(self, db):
        project = await _make_project(db)
        assert await crud.get_module_tree(db, project.id) == []

    @pytest.mark.asyncio
    async def test_flat_tree(self, db):
        project = await _make_project(db)
        await _make_module(db, project.id, "a")
        await _make_module(db, project.id, "b")
        tree = await crud.get_module_tree(db, project.id)
        assert len(tree) == 2
        for node in tree:
            assert node["children"] == []
            assert "created_at" in node

    @pytest.mark.asyncio
    async def test_nested_tree(self, db):
        """父-子-孙 三级树形结构应被正确构建。"""
        project = await _make_project(db)
        root = await _make_module(db, project.id, "root")
        child1 = await _make_module(db, project.id, "child1", parent_id=root.id)
        child2 = await _make_module(db, project.id, "child2", parent_id=root.id)
        grand = await _make_module(db, project.id, "grand", parent_id=child1.id)
        tree = await crud.get_module_tree(db, project.id)
        assert len(tree) == 1
        root_node = tree[0]
        assert root_node["name"] == "root"
        child_names = sorted(c["name"] for c in root_node["children"])
        assert child_names == ["child1", "child2"]
        grand_parent = next(
            c for c in root_node["children"] if c["name"] == "child1"
        )
        assert len(grand_parent["children"]) == 1
        assert grand_parent["children"][0]["name"] == "grand"
        assert grand_parent["children"][0]["parent_id"] == child1.id
        assert grand.id  # 触发 grand.id 的引用

    @pytest.mark.asyncio
    async def test_tree_node_payload_shape(self, db):
        """每个节点应包含 id/name/project_id/parent_id/description/created_at/children。"""
        project = await _make_project(db)
        m = await _make_module(db, project.id, "n", description="d")
        tree = await crud.get_module_tree(db, project.id)
        assert tree[0]["id"] == m.id
        assert tree[0]["name"] == "n"
        assert tree[0]["project_id"] == project.id
        assert tree[0]["description"] == "d"
        assert tree[0]["parent_id"] is None

    @pytest.mark.asyncio
    async def test_tree_orphan_child_excluded(self, db):
        """parent_id 指向不存在的模块（孤儿）应作为根节点。

        通过临时关闭 SQLite 外键约束构造孤儿数据。
        """
        from sqlalchemy import text
        project = await _make_project(db)
        m = await _make_module(db, project.id, "m")
        await db.execute(text("PRAGMA foreign_keys = OFF"))
        try:
            m.parent_id = 99999
            await db.commit()
            tree = await crud.get_module_tree(db, project.id)
        finally:
            await db.execute(text("PRAGMA foreign_keys = ON"))
            m.parent_id = None
            await db.commit()
        assert len(tree) == 1
        assert tree[0]["id"] == m.id


class TestModuleDescendants:
    """覆盖 get_module_descendants（递归收集所有下级 ID）。"""

    @pytest.mark.asyncio
    async def test_leaf_module(self, db):
        project = await _make_project(db)
        m = await _make_module(db, project.id, "leaf")
        assert await crud.get_module_descendants(db, m.id) == [m.id]

    @pytest.mark.asyncio
    async def test_with_children_and_grandchildren(self, db):
        project = await _make_project(db)
        root = await _make_module(db, project.id, "root")
        c1 = await _make_module(db, project.id, "c1", parent_id=root.id)
        c2 = await _make_module(db, project.id, "c2", parent_id=root.id)
        g1 = await _make_module(db, project.id, "g1", parent_id=c1.id)
        ids = sorted(crud.get_module_descendants(db, root.id))
        assert ids == sorted([root.id, c1.id, c2.id, g1.id])


class TestValidateModuleParent:
    """覆盖 validate_module_parent（line 77, 88, 90）。"""

    @pytest.mark.asyncio
    async def test_none_parent_is_valid(self, db):
        project = await _make_project(db)
        m = await _make_module(db, project.id, "m")
        assert await crud.validate_module_parent(db, m.id, None) is True

    @pytest.mark.asyncio
    async def test_self_as_parent_is_invalid(self, db):
        """parent_id == module_id 形成自环 → False。"""
        project = await _make_project(db)
        m = await _make_module(db, project.id, "m")
        assert await crud.validate_module_parent(db, m.id, m.id) is False

    @pytest.mark.asyncio
    async def test_valid_chain(self, db):
        """parent 链路不形成环 → True。"""
        project = await _make_project(db)
        a = await _make_module(db, project.id, "a")
        b = await _make_module(db, project.id, "b", parent_id=a.id)
        c = await _make_module(db, project.id, "c", parent_id=b.id)
        assert await crud.validate_module_parent(db, c.id, a.id) is True

    @pytest.mark.asyncio
    async def test_cycle_in_chain_is_invalid(self, db):
        """链路中存在回到自身的环 → False。"""
        project = await _make_project(db)
        a = await _make_module(db, project.id, "a")
        b = await _make_module(db, project.id, "b", parent_id=a.id)
        c = await _make_module(db, project.id, "c", parent_id=b.id)
        a.parent_id = c.id
        await db.commit()
        assert await crud.validate_module_parent(db, a.id, c.id) is False

    @pytest.mark.asyncio
    async def test_missing_parent_breaks_walk_returns_true(self, db):
        project = await _make_project(db)
        m = await _make_module(db, project.id, "m")
        assert await crud.validate_module_parent(db, m.id, 99999) is True

    @pytest.mark.asyncio
    async def test_repeated_node_visited_returns_false(self, db):
        project = await _make_project(db)
        a = await _make_module(db, project.id, "a")
        b = await _make_module(db, project.id, "b", parent_id=a.id)
        b.parent_id = b.id
        await db.commit()
        assert await crud.validate_module_parent(db, a.id, b.id) is False


class TestDeleteModuleProtection:
    """覆盖 delete_module 中的 case_count > 0 保护与级联删除。"""

    @pytest.mark.asyncio
    async def test_delete_module_with_cases_raises_value_error(self, db):
        """模块下存在测试用例时，删除应抛出 ValueError。"""
        project = await _make_project(db)
        m = await _make_module(db, project.id, "m")
        await crud.create_test_case(db, models.TestCaseCreate(
            project_id=project.id, module_id=m.id, name="c",
            steps=[TestStepCreatePayload(step_order=1, description="s")],
        ))
        with pytest.raises(ValueError, match="测试用例"):
            await crud.delete_module(db, m.id)

    @pytest.mark.asyncio
    async def test_delete_module_cascades_children(self, db):
        """模块无测试用例时，应级联删除子模块。"""
        project = await _make_project(db)
        parent = await _make_module(db, project.id, "parent")
        child = await _make_module(db, project.id, "child", parent_id=parent.id)
        grand = await _make_module(db, project.id, "grand", parent_id=child.id)
        child_id, grand_id = child.id, grand.id
        assert await crud.delete_module(db, parent.id) is True
        assert await crud.get_module(db, parent.id) is None
        assert await crud.get_module(db, child_id) is None
        assert await crud.get_module(db, grand_id) is None

    @pytest.mark.asyncio
    async def test_delete_module_keeps_unrelated_modules(self, db):
        """删除一个模块不应影响其他模块。"""
        project = await _make_project(db)
        m1 = await _make_module(db, project.id, "m1")
        m2 = await _make_module(db, project.id, "m2")
        await crud.delete_module(db, m1.id)
        assert await crud.get_module(db, m2.id) is not None
